"""The worked examples on the information cards, computed from a real material-plant with the formulas
in force -- so an edited formula is never shown beside an example worked out under the old one.

One real row per KPI. For High Stockout Risk the row is one that *is* at risk when there is one (the
card is about risk); otherwise any judged row. Every example is read-only and costs one small query.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.initiatives.i7.kpi_formulas import expression
from app.initiatives.i7.kpi_formulas.definitions import EXCESS, STOCKOUT, VARIABLES, WORKING_CAPITAL
from app.initiatives.i7.kpi_formulas.resolved import Formulas
from app.initiatives.i7.kpi_formulas.sql import at_risk_sql, excess_condition_sql, working_capital_sql
from app.models.i7_features import MaterialFeature
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedMaterial, StagedStock
from app.models.i7_stockout_risk import StockoutRiskInput

logger = logging.getLogger(__name__)


def fmt(value: Decimal | None) -> str:
    """A number as the card prints it: thousands separators, no trailing zeros."""
    if value is None:
        return "no value"
    places = 1 if abs(value) >= 1000 else 2 if abs(value) >= 1 else 4
    text = f"{value:,.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _substituted(parsed: expression.Parsed, values: dict[str, Decimal | None]) -> str:
    return expression.to_text(parsed, {name: fmt(value) for name, value in values.items() if name in parsed.names})


def _stage_lines(formulas: Formulas, keys: tuple[str, ...], values: dict[str, Any]) -> list[str]:
    """``name = formula = numbers = value`` for each stage, filling ``values`` as it goes."""
    lines = []
    labels = {"avg_daily_use": "Average daily use", "stock_needed": "Stock needed"}
    for key in keys:
        parsed = formulas.parsed[key]
        result = expression.evaluate(parsed, values)
        values[key] = result
        lines.append(f"{labels[key]} = {_substituted(parsed, values)} = {fmt(result)}")  # type: ignore[arg-type]
    return lines


def _describe(session: Session, material: str) -> str:
    description = session.execute(
        select(StagedMaterial.description).where(StagedMaterial.sap_material_number == material).limit(1)
    ).scalar()
    return f"{description} ({material})" if description else material


def stockout_example(session: Session, formulas: Formulas) -> dict | None:
    on_hand = (
        select(
            StagedStock.sap_material_number.label("m"),
            StagedStock.sap_plant_code.label("p"),
            func.sum(StagedStock.unrestricted_use_stock).label("on_hand"),
            func.sum(case((StagedStock.unrestricted_use_stock.is_(None), 1), else_=0)).label("blank"),
        )
        .group_by(StagedStock.sap_material_number, StagedStock.sap_plant_code)
        .subquery()
    )
    usage, feature = StockoutRiskInput, MaterialFeature
    as_of = session.execute(select(func.max(usage.as_of_date))).scalar()
    if as_of is None:
        return None
    judged = and_(
        usage.window_days > 0,
        usage.consumption_90d >= 0,
        feature.lead_time_days > 0,
        on_hand.c.on_hand >= 0,
        on_hand.c.blank == 0,
    )
    risk = at_risk_sql(
        formulas,
        usage_in_window=usage.consumption_90d,
        window_days=usage.window_days,
        lead_time_days=feature.lead_time_days,
        stock_on_hand=on_hand.c.on_hand,
    )
    row = None
    for want_risk in (True, False):
        row = session.execute(
            select(
                usage.sap_material_number,
                usage.sap_plant_code,
                usage.consumption_90d,
                usage.window_days,
                feature.lead_time_days,
                on_hand.c.on_hand,
            )
            .select_from(usage)
            .join(feature, and_(feature.sap_material_number == usage.sap_material_number, feature.sap_plant_code == usage.sap_plant_code))
            .join(on_hand, and_(on_hand.c.m == usage.sap_material_number, on_hand.c.p == usage.sap_plant_code))
            .where(judged, usage.consumption_90d > 0, risk if want_risk else ~risk)
            .order_by(usage.consumption_90d.desc(), usage.sap_material_number)
            .limit(1)
        ).first()
        if row is not None:
            break
    if row is None:
        return None

    material, plant, usage_in_window, window_days, lead_time, stock = row
    values: dict[str, Any] = {
        "usage_in_window": Decimal(usage_in_window),
        "window_days": Decimal(window_days),
        "lead_time_days": Decimal(lead_time),
        "stock_on_hand": Decimal(stock),
    }
    lines = _stage_lines(formulas, ("avg_daily_use", "stock_needed"), values)
    at_risk = expression.evaluate(formulas.parsed["at_risk"], values)
    lines.append(f"{_substituted(formulas.parsed['at_risk'], values)}")
    return {
        "kpi": STOCKOUT,
        "material": _describe(session, material),
        "plant": plant,
        "inputs": [
            {"label": "Stock on hand", "value": fmt(values["stock_on_hand"])},
            {"label": f"Used in last {window_days} days", "value": fmt(values["usage_in_window"])},
            {"label": "Lead time", "value": f"{fmt(values['lead_time_days'])} days"},
        ],
        "substitution": lines,
        "result": (
            "At risk: stock runs out before a new order could arrive."
            if at_risk
            else "Not at risk: stock covers the time a new order takes to arrive."
        ),
        "at_risk": bool(at_risk),
    }


def _recommendation_example(
    session: Session, formulas: Formulas, *, kpi: str, condition: Any, value_key: str, labels: list[tuple[str, str]]
) -> dict | None:
    names = [name for _, name in labels]
    columns = {name: getattr(Recommendation, name) for name in names}
    # The newest recommendation of each material-plant -- the population the Overview counts.
    newest = select(func.max(Recommendation.id)).group_by(
        Recommendation.sap_material_number, Recommendation.sap_plant_code
    )
    statement = select(Recommendation.sap_material_number, Recommendation.sap_plant_code, *columns.values()).where(
        Recommendation.unit_price.is_not(None), Recommendation.id.in_(newest)
    )
    if condition is not None:
        statement = statement.where(condition)
    row = session.execute(
        statement.order_by(Recommendation.id.desc()).limit(1)
    ).first()
    if row is None:
        return None
    material, plant, *figures = row
    values: dict[str, Decimal | None] = {
        name: (Decimal(str(v)) if v is not None else None) for name, v in zip(names, figures)
    }
    parsed = formulas.parsed[value_key]
    result = expression.evaluate(parsed, values)
    return {
        "kpi": kpi,
        "material": _describe(session, material),
        "plant": plant,
        "inputs": [{"label": label, "value": fmt(values[name])} for label, name in labels],
        "substitution": [f"{_substituted(parsed, values)} = {fmt(result)}"],  # type: ignore[arg-type]
        "result": fmt(result),  # type: ignore[arg-type]
    }


def excess_example(session: Session, formulas: Formulas) -> dict | None:
    return _recommendation_example(
        session,
        formulas,
        kpi=EXCESS,
        condition=excess_condition_sql(formulas),
        value_key="excess_value",
        labels=[
            ("Current max stock", "current_max_stock"),
            ("Recommended max stock", "recommended_max_stock"),
            ("Unit price", "unit_price"),
        ],
    )


def working_capital_example(session: Session, formulas: Formulas) -> dict | None:
    return _recommendation_example(
        session,
        formulas,
        kpi=WORKING_CAPITAL,
        condition=working_capital_sql(formulas).is_not(None),
        value_key="working_capital_value",
        labels=[
            ("Current reorder point", "current_rop"),
            ("Recommended reorder point", "recommended_rop"),
            ("Unit price", "unit_price"),
        ],
    )


def build_examples(session: Session, formulas: Formulas) -> dict[str, dict | None]:
    """One example per KPI, or ``None`` when no real row fits (the card then says so)."""
    result: dict[str, dict | None] = {}
    for kpi, builder in (
        (STOCKOUT, stockout_example),
        (EXCESS, excess_example),
        (WORKING_CAPITAL, working_capital_example),
    ):
        try:
            result[kpi] = builder(session, formulas)
        except SQLAlchemyError:
            session.rollback()
            logger.exception("could not build the %s example", kpi)
            result[kpi] = None
    return result


__all__ = ["build_examples", "fmt", "VARIABLES"]

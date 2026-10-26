"""The worked examples on the information cards, computed from a real material-plant with the formulas
in force -- so an edited formula is never shown beside an example worked out under the old one.

One real row per calculation, and the inputs shown are whichever facts the formulas actually use, so an
invented formula gets an example in its own terms. For High Stockout Risk the row is one that *is* at risk
when there is one (the card is about risk); otherwise any judged row. Every example is read-only and costs
one small query.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.initiatives.i7.kpi_formulas import expression
from app.initiatives.i7.kpi_formulas.data_catalog import (
    ADOPTION_ITEMS,
    RECOMMENDATION_ITEMS,
    STOCKOUT_ITEMS,
    DataItem,
    labels,
    recommendation_columns,
    stockout_columns,
)
from app.initiatives.i7.kpi_formulas.definitions import ADOPTION, EXCESS, STOCKOUT, WORKING_CAPITAL
from app.initiatives.i7.kpi_formulas.resolved import Formulas
from app.initiatives.i7.kpi_formulas.sql import excess_condition_sql, stockout_rule, stockout_used_names, working_capital_sql
from app.models.i7_features import MaterialFeature
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedMaterial, StagedStock
from app.models.i7_stockout_risk import StockoutRiskInput

logger = logging.getLogger(__name__)

STAGES = ("avg_daily_use", "stock_needed")


def fmt(value: Decimal | None) -> str:
    """A number as the card prints it: thousands separators, no trailing zeros."""
    if value is None:
        return "no value"
    places = 1 if abs(value) >= 1000 else 2 if abs(value) >= 1 else 4
    text = f"{value:,.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _substituted(parsed: expression.Parsed, values: dict[str, Any]) -> str:
    return expression.to_text(parsed, {name: fmt(value) for name, value in values.items() if name in parsed.names})


def _inputs(items: tuple[DataItem, ...], names: set[str], values: dict[str, Any]) -> list[dict[str, str]]:
    """One line per stored fact the formulas used, in the catalogue's order."""
    rows = []
    for item in items:
        if item.name in names:
            suffix = " days" if item.unit == "days" else ""
            rows.append({"label": item.label[:1].upper() + item.label[1:], "value": f"{fmt(values.get(item.name))}{suffix}"})
    return rows


def _describe(session: Session, material: str) -> str:
    description = session.execute(
        select(StagedMaterial.description).where(StagedMaterial.sap_material_number == material).limit(1)
    ).scalar()
    return f"{description} ({material})" if description else material


def _decimals(row: Any, names: list[str]) -> dict[str, Decimal | None]:
    return {name: (Decimal(str(value)) if value is not None else None) for name, value in zip(names, row)}


def stockout_example(session: Session, formulas: Formulas) -> dict | None:
    on_hand = (
        select(
            StagedStock.sap_material_number.label("m"),
            StagedStock.sap_plant_code.label("p"),
            func.sum(StagedStock.unrestricted_use_stock).label("on_hand"),
            func.sum(case((StagedStock.unrestricted_use_stock.is_(None), 1), else_=0)).label("blank_locations"),
        )
        .group_by(StagedStock.sap_material_number, StagedStock.sap_plant_code)
        .subquery()
    )
    usage, feature = StockoutRiskInput, MaterialFeature
    if session.execute(select(func.max(usage.as_of_date))).scalar() is None:
        return None

    facts = sorted(stockout_used_names(formulas))
    rule, judged = stockout_rule(formulas, usage=usage, feature=feature, on_hand=on_hand)
    columns = stockout_columns(usage=usage, feature=feature, on_hand=on_hand.c.on_hand)

    row = None
    for want_risk in (True, False):
        row = session.execute(
            select(usage.sap_material_number, usage.sap_plant_code, *[columns[name] for name in facts])
            .select_from(usage)
            .outerjoin(
                feature,
                and_(feature.sap_material_number == usage.sap_material_number, feature.sap_plant_code == usage.sap_plant_code),
            )
            .outerjoin(on_hand, and_(on_hand.c.m == usage.sap_material_number, on_hand.c.p == usage.sap_plant_code))
            .where(judged, usage.consumption_90d > 0, rule if want_risk else ~rule)
            .order_by(usage.consumption_90d.desc(), usage.sap_material_number)
            .limit(1)
        ).first()
        if row is not None:
            break
    if row is None:
        return None

    material, plant, *figures = row
    values: dict[str, Any] = _decimals(figures, facts)
    needed = set(formulas.parsed["at_risk"].names)
    for stage in reversed(STAGES):
        if stage in needed:
            needed |= formulas.parsed[stage].names
    lines = []
    for stage in STAGES:
        if stage in needed:
            result = expression.evaluate(formulas.parsed[stage], values)
            values[stage] = result
            lines.append(f"{labels()[stage].capitalize()} = {_substituted(formulas.parsed[stage], values)} = {fmt(result)}")
    at_risk = expression.evaluate(formulas.parsed["at_risk"], values)
    lines.append(_substituted(formulas.parsed["at_risk"], values))
    return {
        "kpi": STOCKOUT,
        "material": _describe(session, material),
        "plant": plant,
        "inputs": _inputs(STOCKOUT_ITEMS, set(facts), values),
        "substitution": lines,
        "result": (
            "At risk: the rule is true for this material."
            if at_risk
            else "Not at risk: the rule is false for this material."
        ),
        "at_risk": bool(at_risk),
    }


def _recommendation_example(
    session: Session, formulas: Formulas, *, kpi: str, condition: Any, value_key: str, extra_key: str | None = None
) -> dict | None:
    parsed = formulas.parsed[value_key]
    names = set(parsed.names)
    if extra_key:
        names |= formulas.parsed[extra_key].names
    ordered = sorted(names)
    columns = recommendation_columns()
    # The newest recommendation of each material-plant -- the population the Overview counts.
    newest = select(func.max(Recommendation.id)).group_by(Recommendation.sap_material_number, Recommendation.sap_plant_code)
    statement = select(
        Recommendation.sap_material_number, Recommendation.sap_plant_code, *[columns[n] for n in ordered]
    ).where(Recommendation.id.in_(newest))
    if condition is not None:
        statement = statement.where(condition)
    row = session.execute(statement.order_by(Recommendation.id.desc()).limit(1)).first()
    if row is None:
        return None
    material, plant, *figures = row
    values = _decimals(figures, ordered)
    result = expression.evaluate(parsed, values)
    return {
        "kpi": kpi,
        "material": _describe(session, material),
        "plant": plant,
        "inputs": _inputs(RECOMMENDATION_ITEMS, names, values),
        "substitution": [f"{_substituted(parsed, values)} = {fmt(result)}"],  # type: ignore[arg-type]
        "result": fmt(result),  # type: ignore[arg-type]
    }


def excess_example(session: Session, formulas: Formulas) -> dict | None:
    return _recommendation_example(
        session, formulas, kpi=EXCESS, condition=excess_condition_sql(formulas), value_key="excess_value",
        extra_key="excess_condition",
    )


def working_capital_example(session: Session, formulas: Formulas) -> dict | None:
    return _recommendation_example(
        session, formulas, kpi=WORKING_CAPITAL, condition=working_capital_sql(formulas).is_not(None),
        value_key="working_capital_value",
    )


def adoption_example(session: Session, formulas: Formulas) -> dict | None:
    from app.api.i7.adoption import adoption_counts

    counts = adoption_counts(session)
    if counts.tracked == 0:
        return None
    values: dict[str, Any] = {
        "adopted": Decimal(counts.adopted),
        "partially_adopted": Decimal(counts.partially_adopted),
        "not_adopted": Decimal(counts.not_adopted),
        "pending": Decimal(counts.pending),
        "tracked": Decimal(counts.tracked),
    }
    rate = expression.evaluate(formulas.parsed["adoption_rate"], values)
    card = expression.evaluate(formulas.parsed["adopted_card"], values)
    names = formulas.parsed["adoption_rate"].names | formulas.parsed["adopted_card"].names
    return {
        "kpi": ADOPTION,
        "material": f"All {counts.tracked:,} recommendations with something to adopt",
        "plant": "All plants",
        "inputs": _inputs(ADOPTION_ITEMS, set(names), values),
        "substitution": [
            f"Adoption rate = {_substituted(formulas.parsed['adoption_rate'], values)} = "
            f"{'not yet measured' if not isinstance(rate, Decimal) else f'{rate * 100:.1f}%'}",
            f"Adopted card = {_substituted(formulas.parsed['adopted_card'], values)} = "
            f"{'not available' if not isinstance(card, Decimal) else fmt(card)}",
        ],
        "result": (
            "Not yet measured: nothing to divide by yet."
            if not isinstance(rate, Decimal)
            else f"{rate * 100:.1f}% adoption rate; the card counts {fmt(card) if isinstance(card, Decimal) else 'nothing'}."
        ),
    }


def build_examples(session: Session, formulas: Formulas) -> dict[str, dict | None]:
    """One example per calculation, or ``None`` when no real row fits (the card then says so)."""
    result: dict[str, dict | None] = {}
    for kpi, builder in (
        (STOCKOUT, stockout_example),
        (EXCESS, excess_example),
        (WORKING_CAPITAL, working_capital_example),
        (ADOPTION, adoption_example),
    ):
        try:
            result[kpi] = builder(session, formulas)
        except SQLAlchemyError:
            session.rollback()
            logger.exception("could not build the %s example", kpi)
            result[kpi] = None
    return result


__all__ = ["build_examples", "fmt"]

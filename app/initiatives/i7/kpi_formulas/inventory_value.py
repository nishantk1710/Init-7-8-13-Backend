"""Stock held, in ZAR: inventory value, safety stock value and the material-plants below their reorder point.

Three figures over one population (the caller's ``select(Recommendation)``, the same one excess and working capital
read), so they can be set against each other.

* **Stock on hand** is unrestricted-use stock summed over storage locations (``i7_staged_stock``), as the stockout
  rule reads it. A material-plant with no stock row, a negative stock, or a storage location with no quantity has no
  stock figure -- a partly blank total is not a stock figure -- and is left out, never counted as 0.
* **Price and currency** are MBEW's, exactly as :mod:`working_capital` reads them: a currency that
  :data:`~.currency.TO_ZAR` can state in ZAR, and a price above the placeholder ceiling. Nothing else is converted.
* **Safety stock value** uses the safety stock SAP holds (the value stored on the recommendation), not the recommended
  one. **Below reorder point** compares stock on hand with the reorder point SAP holds; a reorder point of 0 is SAP's
  "not maintained" and cannot be judged, the same reading the stockout rule gives a lead time of 0.

Every figure comes with how many material-plants it is over; the rest are left out and the counts say so.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Select, and_, case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.initiatives.i7.kpi_formulas.currency import REPORTING_CURRENCY, TO_ZAR
from app.initiatives.i7.kpi_formulas.working_capital import _price_currency
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedStock, StagingRun
from app.shared.valuation_price import DEFAULT_TOKEN_MAX


@dataclass(frozen=True)
class InventoryMeasurements:
    currency: str
    inventory_value: Decimal | None
    inventory_priced_count: int
    safety_stock_value: Decimal | None
    safety_stock_priced_count: int
    below_reorder_count: int | None
    below_reorder_judged: int


def stock_loaded_at(session: Session) -> datetime | None:
    """When stock on hand was last loaded (the latest staging run that staged any); ``None`` when never."""
    try:
        return session.execute(
            select(func.max(StagingRun.finished_at)).where(StagingRun.stock_staged > 0, StagingRun.finished_at.isnot(None))
        ).scalar()
    except SQLAlchemyError:
        session.rollback()
        return None


def stock_date_problem(loaded_at: datetime | None, cutoff: date, max_age_days: int) -> str | None:
    """Why the stock behind a report cannot speak for its cutoff, or ``None`` when it can."""
    if loaded_at is None:
        return "No stock-on-hand load is recorded, so the stock held cannot be valued or dated."
    gap = abs((cutoff - loaded_at.date()).days)
    if gap > max_age_days:
        return (
            f"Stock on hand was loaded on {loaded_at:%d %b %Y}, {gap} days from this report's cutoff ({cutoff:%d %b %Y}); "
            "it is not shown as the position for the period."
        )
    return None


def inventory_measurements(session: Session, base: Select) -> InventoryMeasurements:
    """The three figures over ``base`` (a ``select(Recommendation)`` population)."""
    rate = case(*[(_price_currency() == code, factor) for code, factor in TO_ZAR.items()], else_=None)
    on_hand = (
        select(
            StagedStock.sap_material_number.label("sap_material_number"),
            StagedStock.sap_plant_code.label("sap_plant_code"),
            func.sum(StagedStock.unrestricted_use_stock).label("on_hand"),
            func.sum(case((StagedStock.unrestricted_use_stock.is_(None), 1), else_=0)).label("blank_locations"),
        )
        .group_by(StagedStock.sap_material_number, StagedStock.sap_plant_code)
        .subquery()
    )
    priced = and_(rate.is_not(None), Recommendation.unit_price > DEFAULT_TOKEN_MAX)
    has_stock = and_(on_hand.c.on_hand.is_not(None), on_hand.c.blank_locations == 0, on_hand.c.on_hand >= 0)
    held = and_(priced, has_stock)
    safety = and_(priced, Recommendation.current_safety_stock.is_not(None), Recommendation.current_safety_stock >= 0)
    judged = and_(has_stock, Recommendation.current_rop > 0)

    # Each row's own figures first, then the sums: SQL Server refuses an aggregate over an expression that holds a
    # subquery (the currency lookup).
    per_row = (
        base.with_only_columns(
            case((held, 1), else_=0).label("held"),
            case((held, on_hand.c.on_hand * Recommendation.unit_price * rate), else_=None).label("held_zar"),
            case((safety, 1), else_=0).label("safe"),
            case((safety, Recommendation.current_safety_stock * Recommendation.unit_price * rate), else_=None).label("safe_zar"),
            case((judged, 1), else_=0).label("judged"),
            case((and_(judged, on_hand.c.on_hand < Recommendation.current_rop), 1), else_=0).label("below"),
        )
        .select_from(Recommendation)
        .outerjoin(
            on_hand,
            and_(
                on_hand.c.sap_material_number == Recommendation.sap_material_number,
                on_hand.c.sap_plant_code == Recommendation.sap_plant_code,
            ),
        )
        .subquery()
    )
    held_n, held_value, safe_n, safe_value, judged_n, below_n = session.execute(
        select(
            func.coalesce(func.sum(per_row.c.held), 0),
            func.sum(per_row.c.held_zar),
            func.coalesce(func.sum(per_row.c.safe), 0),
            func.sum(per_row.c.safe_zar),
            func.coalesce(func.sum(per_row.c.judged), 0),
            func.coalesce(func.sum(per_row.c.below), 0),
        )
    ).one()
    return InventoryMeasurements(
        currency=REPORTING_CURRENCY,
        inventory_value=Decimal(str(held_value)) if held_value is not None else None,
        inventory_priced_count=int(held_n),
        safety_stock_value=Decimal(str(safe_value)) if safe_value is not None else None,
        safety_stock_priced_count=int(safe_n),
        # No judgeable material-plant is "not measured", never 0 below.
        below_reorder_count=int(below_n) if int(judged_n) else None,
        below_reorder_judged=int(judged_n),
    )

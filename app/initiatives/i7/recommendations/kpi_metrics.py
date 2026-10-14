"""The Overview's headline KPIs, in one place.

These are the calculations behind the Overview's "Critical stockout risk", "Excess inventory"
and "Net safety stock value" figures. They used to live inline in the summary endpoint; the
trend snapshots need exactly the same numbers, so they live here and both call them. Nothing
about the definitions changed in the move -- the summary endpoint's tests pin that.

Every function takes ``base``: a ``select(Recommendation)`` already restricted to the newest
recommendation per material-plant (and to whatever filters or plant scope the caller wants).
The caller decides the population; these decide what counts within it.

**The definitions are internal proxies (2026-09-22), not Vedanta-confirmed KPIs.** They are kept
exactly as they were so a trend agrees with the number on the Overview. Business confirmation is
pending; if a definition changes it changes here, once.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedMaterial, StagedStock


@dataclass(frozen=True)
class KpiMetrics:
    total: int
    stockout_risk_count: int
    excess_count: int
    excess_opportunity: Decimal | None
    net_safety_stock_value_impact: Decimal | None


def latest_per_material_plant(*, up_to_id: int | None = None):
    """The id of the newest recommendation of each material-plant, as a subquery.

    ``id`` is an auto-increment, so a later generation's row always has a higher id: the same
    answer as ranking by ``generated_at DESC, id DESC``, in a form the database plans well.
    ``up_to_id`` gives the picture *as of* a past generation (the newest row at or below that
    id) -- how reconstructed history is built.
    """
    statement = select(func.max(Recommendation.id).label("id"))
    if up_to_id is not None:
        statement = statement.where(Recommendation.id <= up_to_id)
    return statement.group_by(Recommendation.sap_material_number, Recommendation.sap_plant_code).subquery()


def base_for_scope(plant: str | None, *, up_to_id: int | None = None) -> Select:
    """``select(Recommendation)`` over the newest row per material-plant, for one plant or all."""
    latest = latest_per_material_plant(up_to_id=up_to_id)
    base = select(Recommendation).join(latest, latest.c.id == Recommendation.id)
    if plant is not None:
        base = base.where(Recommendation.sap_plant_code == plant)
    return base


def count_rows(session: Session, base: Select) -> int:
    return session.execute(select(func.count()).select_from(base.subquery())).scalar_one()


def net_safety_stock_value_impact(session: Session, base: Select) -> Decimal | None:
    """SUM(unit_price * (current_safety_stock - recommended_safety_stock)) where all three exist.
    NULL when no row has them -- never 0."""
    return session.execute(
        base.with_only_columns(
            func.sum(
                Recommendation.unit_price
                * (Recommendation.current_safety_stock - Recommendation.recommended_safety_stock)
            )
        ).where(
            Recommendation.unit_price.isnot(None),
            Recommendation.current_safety_stock.isnot(None),
            Recommendation.recommended_safety_stock.isnot(None),
        )
    ).scalar()


def stockout_risk_count(session: Session, base: Select) -> int:
    """Material-plants whose current on-hand stock is below ``recommended_rop``.

    ``i7_staged_stock`` is material-plant-storage-location grain, so it is summed to
    material-plant first (``unrestricted_use_stock`` only -- see StagedStock). A material-plant
    with no staged stock, or no recommended ROP, is counted as neither at risk nor safe.

    This reads the stock **as it is now**. That is why it can only be recorded at the moment a
    generation finishes and cannot be reconstructed for an earlier one.
    """
    on_hand = (
        select(
            StagedStock.sap_material_number,
            StagedStock.sap_plant_code,
            func.sum(StagedStock.unrestricted_use_stock).label("on_hand"),
        )
        .group_by(StagedStock.sap_material_number, StagedStock.sap_plant_code)
        .subquery()
    )
    at_risk = base.join(
        on_hand,
        (on_hand.c.sap_material_number == Recommendation.sap_material_number)
        & (on_hand.c.sap_plant_code == Recommendation.sap_plant_code),
    ).where(
        Recommendation.recommended_rop.isnot(None),
        on_hand.c.on_hand < Recommendation.recommended_rop,
    )
    return count_rows(session, at_risk)


def excess_base(base: Select) -> Select:
    """The excess-inventory candidates: any positive gap, no minimum margin."""
    return base.where(
        Recommendation.current_max_stock.isnot(None),
        Recommendation.recommended_max_stock.isnot(None),
        Recommendation.current_max_stock > Recommendation.recommended_max_stock,
    )


def excess_opportunity(session: Session, base: Select) -> Decimal | None:
    """SUM(unit_price * (current_max_stock - recommended_max_stock)) over the excess rows that
    have a price. NULL when none does -- never 0."""
    return session.execute(
        excess_base(base)
        .with_only_columns(
            func.sum(
                Recommendation.unit_price
                * (Recommendation.current_max_stock - Recommendation.recommended_max_stock)
            )
        )
        .where(Recommendation.unit_price.isnot(None))
    ).scalar()


def portfolio_currency(session: Session) -> str | None:
    """The one currency every priced material is in, or ``None``.

    A portfolio total in mixed currencies has no single true symbol, so it reports none rather
    than the first one found.
    """
    found = (
        session.execute(
            select(StagedMaterial.currency)
            .where(StagedMaterial.currency.is_not(None), StagedMaterial.unit_price.is_not(None))
            .distinct()
            .limit(2)
        )
        .scalars()
        .all()
    )
    return found[0] if len(found) == 1 else None


def compute_kpi_metrics(session: Session, base: Select) -> KpiMetrics:
    """All the headline figures for one population."""
    return KpiMetrics(
        total=count_rows(session, base),
        stockout_risk_count=stockout_risk_count(session, base),
        excess_count=count_rows(session, excess_base(base)),
        excess_opportunity=excess_opportunity(session, base),
        net_safety_stock_value_impact=net_safety_stock_value_impact(session, base),
    )

"""The High Stockout Risk counts, in SQL, over whichever recommendations the page is
looking at.

For every material-plant in ``base`` (the latest recommendation per material-plant,
narrowed by the caller's filters):

* **Not part of this KPI** -- Non-moving: its last issue is more than 730 days before
  the anchor, or it has never been issued in a plant whose movements were loaded.
* **Insufficient Data** -- it is Fast or Slow moving but has no lead time or no stock
  on hand, or it sits in a plant with no movement data at all (so "never issued"
  would be a guess).
* **Evaluated** -- Fast or Slow moving, with a lead time and a stock on hand.
* **At risk** -- evaluated, and stock on hand is below average daily consumption x lead
  time. Compared as ``SOH x window_days < consumption x lead time``, which is the same
  test without dividing.

See :mod:`app.initiatives.i7.stockout_risk` for the definitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Select, and_, case, exists, func, or_, select, union_all
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.initiatives.i7.smooth_demand import SMOOTH_DEMAND_CLASS
from app.initiatives.i7.stockout_risk import SLOW_MAX_DAYS
from app.models.i7_features import MaterialFeature
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedStock
from app.models.i7_stockout_risk import StockoutRiskInput


@dataclass(frozen=True)
class StockoutRiskCounts:
    at_risk: int
    """Evaluated material-plants whose stock on hand is below the stock they need."""

    evaluated: int
    """At risk plus not at risk: the material-plants that could be judged."""

    insufficient_data: int
    """Fast or Slow material-plants that could not be judged for want of a lead time or
    stock on hand, plus those in a plant with no movement data."""

    as_of: date | None
    """The date the usage was read as of. ``None`` when the usage table has not been
    built -- then every count is 0 and means "not computed", not "nothing at risk"."""


def _smooth_rows(
    recommendation_keys,
    *,
    plant: str | None,
    material: str | None,
    demand_class: str | None,
    criticality: str | None,
    status: str | None,
    is_oar: bool | None,
    confidence: str | None,
) -> Select | None:
    """The SMOOTH material-plants, read from the feature store, or ``None`` when the caller's
    filters rule them out.

    A SMOOTH material-plant has no recommendation (see ``app.initiatives.i7.smooth_demand``), so
    the recommendations cannot supply it. Its lead time is the feature store's resolved one --
    the figure forecasting and inventory use for every other material. The filters that mean
    something for it -- plant, material, demand class, criticality tier -- apply to the feature
    row; a filter that only a recommendation has (status, a confidence, being OAR) leaves no
    SMOOTH material-plant to count.
    """
    if status is not None or confidence is not None or is_oar is True:
        return None
    if demand_class is not None and demand_class != SMOOTH_DEMAND_CLASS:
        return None

    statement = select(
        MaterialFeature.sap_material_number.label("sap_material_number"),
        MaterialFeature.sap_plant_code.label("sap_plant_code"),
        MaterialFeature.lead_time_days.label("lead_time_days"),
    ).where(
        MaterialFeature.demand_class == SMOOTH_DEMAND_CLASS,
        MaterialFeature.history_status == "SUFFICIENT",
        # One row per material-plant: if its latest recommendation is still in the list (it was
        # written before the material was classified SMOOTH), that row already counts it.
        ~exists().where(
            recommendation_keys.c.sap_material_number == MaterialFeature.sap_material_number,
            recommendation_keys.c.sap_plant_code == MaterialFeature.sap_plant_code,
        ),
    )
    if plant is not None:
        statement = statement.where(MaterialFeature.sap_plant_code == plant)
    if material is not None:
        statement = statement.where(MaterialFeature.sap_material_number == material)
    if criticality is not None:
        statement = statement.where(MaterialFeature.criticality == criticality)
    return statement


def stockout_risk_counts(
    session: Session,
    base: Select,
    *,
    plant: str | None = None,
    material: str | None = None,
    demand_class: str | None = None,
    criticality: str | None = None,
    status: str | None = None,
    is_oar: bool | None = None,
    confidence: str | None = None,
) -> StockoutRiskCounts:
    """The counts for the recommendations ``base`` selects, plus the SMOOTH material-plants.

    ``base`` must select from ``Recommendation`` (it is re-projected here), the way the summary
    endpoint's own ``base`` does, and must already leave SMOOTH recommendations out. The keyword
    arguments are the filters ``base`` was built with, so the SMOOTH material-plants -- which
    come from the feature store, not from ``base`` -- are narrowed the same way.
    """
    try:
        as_of = session.execute(select(func.max(StockoutRiskInput.as_of_date))).scalar()
    except SQLAlchemyError:
        # The usage table does not exist yet: the migration has not been applied to this database (a
        # deploy does not run migrations unless told to). That is "not computed", the same as an
        # empty table -- it must not take the whole summary, and with it the Overview, down.
        session.rollback()
        return StockoutRiskCounts(at_risk=0, evaluated=0, insufficient_data=0, as_of=None)
    if as_of is None:
        return StockoutRiskCounts(at_risk=0, evaluated=0, insufficient_data=0, as_of=None)

    recommendation_rows = base.with_only_columns(
        Recommendation.sap_material_number.label("sap_material_number"),
        Recommendation.sap_plant_code.label("sap_plant_code"),
        Recommendation.lead_time_days.label("lead_time_days"),
    )
    smooth_rows = _smooth_rows(
        recommendation_rows.subquery(),
        plant=plant,
        material=material,
        demand_class=demand_class,
        criticality=criticality,
        status=status,
        is_oar=is_oar,
        confidence=confidence,
    )
    rows = (
        union_all(recommendation_rows, smooth_rows) if smooth_rows is not None else recommendation_rows
    ).subquery()

    # Summed over storage locations: a material-plant's stock on hand is its unrestricted-use
    # stock everywhere it is held (NULL when it has no staged stock row, never 0).
    on_hand = (
        select(
            StagedStock.sap_material_number,
            StagedStock.sap_plant_code,
            func.sum(StagedStock.unrestricted_use_stock).label("on_hand"),
        )
        .group_by(StagedStock.sap_material_number, StagedStock.sap_plant_code)
        .subquery()
    )

    # A plant "has movement data" when at least one of its material-plants has been issued. Joined, not
    # tested with NOT IN (SELECT ...): SQL Server refuses a subquery inside an aggregate, and the
    # counts below are SUM(CASE ...).
    plants_with_movements = select(StockoutRiskInput.sap_plant_code.label("sap_plant_code")).distinct().subquery()

    usage = StockoutRiskInput
    has_usage = usage.id.is_not(None)
    fast_or_slow = and_(has_usage, usage.last_issue_date >= as_of - timedelta(days=SLOW_MAX_DAYS))
    has_inputs = and_(rows.c.lead_time_days.is_not(None), on_hand.c.on_hand.is_not(None))

    evaluated = and_(fast_or_slow, has_inputs)
    at_risk = and_(
        evaluated,
        on_hand.c.on_hand * usage.window_days < usage.consumption_90d * rows.c.lead_time_days,
    )
    insufficient = or_(
        and_(fast_or_slow, ~has_inputs),
        and_(~has_usage, plants_with_movements.c.sap_plant_code.is_(None)),
    )

    counts = session.execute(
        select(
            func.coalesce(func.sum(case((at_risk, 1), else_=0)), 0),
            func.coalesce(func.sum(case((evaluated, 1), else_=0)), 0),
            func.coalesce(func.sum(case((insufficient, 1), else_=0)), 0),
        )
        .select_from(rows)
        .outerjoin(
            usage,
            and_(
                usage.sap_material_number == rows.c.sap_material_number,
                usage.sap_plant_code == rows.c.sap_plant_code,
            ),
        )
        .outerjoin(
            on_hand,
            and_(
                on_hand.c.sap_material_number == rows.c.sap_material_number,
                on_hand.c.sap_plant_code == rows.c.sap_plant_code,
            ),
        )
        .outerjoin(plants_with_movements, plants_with_movements.c.sap_plant_code == rows.c.sap_plant_code)
    ).one()
    return StockoutRiskCounts(
        at_risk=int(counts[0]),
        evaluated=int(counts[1]),
        insufficient_data=int(counts[2]),
        as_of=as_of,
    )

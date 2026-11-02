"""The High Stockout Risk counts, in SQL, from the material-plants' own usage, stock and lead time.

The population is every **Fast or Slow moving material-plant in the usage table**
(``i7_stockout_risk_input``), whether or not a recommendation exists for it: the KPI is a statement
about consumption against stock, and which materials the pipeline happened to recommend is not part
of it. Each one is judged on three figures, all read from their own source:

* consumption -- the usage table (net 90-day issues, on the anchor date's window);
* stock on hand -- ``i7_staged_stock`` summed over storage locations;
* lead time -- ``i7_material_feature.lead_time_days``, the resolved value (the Initiative 11 program,
  else MARC planned delivery time), never the recommendation's.

A material-plant is then one of:

* **At risk** -- all three are valid and stock on hand is below average daily consumption x lead time.
  By default ``SOH < (consumption / window_days) x lead time``; the three stages (average daily use,
  stock needed, at risk) are the editable formulas of :mod:`app.initiatives.i7.kpi_formulas`.
* **Evaluated, not at risk** -- all three are valid and stock covers the lead time.
* **Insufficient Data** -- Fast or Slow, but one of the three is missing or invalid: no feature-store
  row or no lead time, a lead time of zero or less (SAP's "not maintained" is 0, and taking it as a real
  lead time would make the requirement 0 and the material safe), no stock row, a negative stock, a
  storage location with no quantity (the sum would silently leave it out), or unusable consumption. It
  is never counted as safe, and no missing value is read as zero.
* **Not part of this KPI** -- Non-moving: its last issue is more than 730 days before the anchor.

One more case is Insufficient Data: a *recommended* material-plant in a plant with no movement data at
all, because "never issued" is only a fact where the movements were loaded.

See :mod:`app.initiatives.i7.stockout_risk` for the definitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Select, and_, case, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.initiatives.i7.kpi_formulas.resolved import Formulas, load_active
from app.initiatives.i7.kpi_formulas.sql import stockout_rule
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
    """Fast or Slow material-plants that could not be judged for want of a valid consumption, stock on
    hand or lead time, plus recommended ones in a plant with no movement data."""

    as_of: date | None
    """The date the usage was read as of. ``None`` when the usage table has not been
    built -- then every count is 0 and means "not computed", not "nothing at risk"."""


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
    formulas: Formulas | None = None,
) -> StockoutRiskCounts:
    """The counts for the Fast and Slow material-plants the caller's filters leave in.

    ``base`` must select from ``Recommendation`` (it is re-projected here), the way the summary
    endpoint's own ``base`` does. The keyword arguments are the filters ``base`` was built with. Plant,
    material, demand class and criticality narrow the usage population directly; a filter only a
    recommendation has (status, a confidence, OAR or not) restricts it to the material-plants ``base``
    selects, since only they have those properties.

    ``formulas`` are the KPI formulas in force (the active saved version, or the built-in defaults when
    none was saved); omitted, they are loaded here. They decide who is *at risk* and how far back a last
    issue may be -- the validity guard below is not editable.
    """
    formulas = formulas or load_active(session)
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

    usage = StockoutRiskInput
    feature = MaterialFeature

    # Summed over storage locations: a material-plant's stock on hand is its unrestricted-use stock
    # everywhere it is held. ``blank_locations`` counts the locations with no quantity, which SUM would
    # skip without a word -- a partly blank total is not a stock figure.
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
    recommended = base.with_only_columns(
        Recommendation.sap_material_number.label("sap_material_number"),
        Recommendation.sap_plant_code.label("sap_plant_code"),
    ).distinct().subquery()  # one row per material-plant, however many recommendations it has

    # Every figure the rule uses must be present and usable. A missing one makes the rule unknown, which the
    # CASEs below count as "not evaluated" -- never as safe. See ``stockout_rule`` for what that means.
    rule, evaluated = stockout_rule(formulas, usage=usage, feature=feature, on_hand=on_hand)
    at_risk = and_(evaluated, rule)

    source = (
        select(
            func.count(),
            func.coalesce(func.sum(case((at_risk, 1), else_=0)), 0),
            func.coalesce(func.sum(case((evaluated, 1), else_=0)), 0),
        )
        .select_from(usage)
        .outerjoin(
            feature,
            and_(
                feature.sap_material_number == usage.sap_material_number,
                feature.sap_plant_code == usage.sap_plant_code,
            ),
        )
        .outerjoin(
            on_hand,
            and_(
                on_hand.c.sap_material_number == usage.sap_material_number,
                on_hand.c.sap_plant_code == usage.sap_plant_code,
            ),
        )
    )
    if status is not None or confidence is not None or is_oar is not None:
        source = source.join(
            recommended,
            and_(
                recommended.c.sap_material_number == usage.sap_material_number,
                recommended.c.sap_plant_code == usage.sap_plant_code,
            ),
        )
    # Fast (last issue 0-365 days before the anchor) and Slow (366-730) by default: measured from the
    # anchor the table was built with, so a changed setting that has not been rebuilt never mislabels
    # the figures.
    source = source.where(usage.last_issue_date >= as_of - timedelta(days=formulas.lookback_days))
    if plant is not None:
        source = source.where(usage.sap_plant_code == plant)
    if material is not None:
        source = source.where(usage.sap_material_number == material)
    if demand_class is not None:
        source = source.where(feature.demand_class == demand_class)
    if criticality is not None:
        source = source.where(feature.criticality == criticality)

    eligible, at_risk_count, evaluated_count = session.execute(source).one()

    # A plant "has movement data" when at least one of its material-plants has been issued. Joined, not
    # tested with NOT IN (SELECT ...): SQL Server refuses a subquery inside an aggregate.
    plants_with_movements = select(usage.sap_plant_code.label("sap_plant_code")).distinct().subquery()
    unjudgeable_plant = session.execute(
        select(func.count())
        .select_from(recommended)
        .outerjoin(plants_with_movements, plants_with_movements.c.sap_plant_code == recommended.c.sap_plant_code)
        .where(plants_with_movements.c.sap_plant_code.is_(None))
    ).scalar_one()

    return StockoutRiskCounts(
        at_risk=int(at_risk_count),
        evaluated=int(evaluated_count),
        insufficient_data=int(eligible) - int(evaluated_count) + int(unjudgeable_plant),
        as_of=as_of,
    )

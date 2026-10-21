"""The Quarterly Report's KPI figures, defined once.

Every number the management view shows for the period -- the cards, the charts, the PDF and the Excel export
-- is computed here and stored in the report; nothing downstream recalculates it. The definitions are the
Inventory Planning Overview's own, so the report and the Overview cannot disagree about a material-plant:

* stockout risk is :func:`app.initiatives.i7.stockout_risk.counts.stockout_risk_counts` (stock on hand below
  average daily consumption x lead time, Fast / Slow movers only, a missing figure is Insufficient Data and is
  never counted as safe);
* excess inventory is a current max stock above the recommended max stock;
* working capital is on the reorder point: ``unit_price x (current ROP - recommended ROP)``, an estimate of the
  recommendations' effect and never a realised saving.

**Two populations, and why.**

* *Recommendation records generated in the period* -- every ``i7_recommendation`` row whose ``generated_at`` is
  inside the half-open period. This is what "recommendations generated" and the status distribution count.
* *The newest recommendation of each material-plant generated in the period* -- what the position figures (stockout,
  excess, working capital) count, so a material-plant regenerated within the period is not counted twice. This is
  the Overview's own "latest per material-plant" rule, applied inside the period.

**What kind of figure each one is** (see ``MetricKind``): recommendations generated is an EVENT in the period;
pending approval is a STATUS AS OF the cutoff; stockout risk is a MEASUREMENT on the usage build date; excess and
working capital are measured on the date the report was generated from the recommendations' own values.

**Stale measurements.** ``i7_stockout_risk_input`` holds one build and no history, so the stockout figure is as of
that build's date. When that date is more than :data:`MAX_MEASUREMENT_AGE_DAYS` from the report's cutoff, the
consumption it rests on is not current for the period and the figure is marked stale -- it is kept for the record
but never presented as the period's position, and never read as "no risk".
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.api.i7.recommendations import _apply_filters, _not_smooth, _portfolio_currency
from app.initiatives.i7.stockout_risk.counts import stockout_risk_counts
from app.models.i7_recommendation import Recommendation
from app.schemas.i7.reports import PositionFigures, StockoutPosition

#: How far from the report's cutoff a stockout measurement can be and still speak for the period.
MAX_MEASUREMENT_AGE_DAYS = 31

#: The statuses in which a recommendation is waiting on someone's decision (the Overview's own awaiting-approval set).
AWAITING_DECISION = ("PENDING_APPROVAL", "HELD")


def _in_period(period_start: date | None, exclusive_end: date):
    """The half-open period. With no ``period_start`` there is no lower bound: everything generated before the end,
    which is what a month-end position is over -- the recommendations in force on that day."""
    upper = Recommendation.generated_at < exclusive_end
    return (Recommendation.generated_at >= period_start, upper) if period_start is not None else (upper,)


def newest_in_period_ids(period_start: date | None, exclusive_end: date):
    """The id of the newest recommendation of each material-plant generated in the period (or, with no
    ``period_start``, before its end). ``id`` is an auto-increment, so the highest id of a material-plant is its
    newest."""
    return (
        select(func.max(Recommendation.id))
        .where(*_in_period(period_start, exclusive_end))
        .group_by(Recommendation.sap_material_number, Recommendation.sap_plant_code)
    )


def count_of(session: Session, statement: Select) -> int:
    return session.execute(select(func.count()).select_from(statement.subquery())).scalar_one()


# --- Counts over the records generated in the period ------------------------------------------------


def records_generated(session: Session, period_start: date, exclusive_end: date) -> int:
    """Recommendation records generated in the period -- rows, not distinct materials."""
    return count_of(session, select(Recommendation.id).where(*_in_period(period_start, exclusive_end)))


def material_plants_assessed(session: Session, period_start: date, exclusive_end: date) -> tuple[int, int]:
    """``(evaluated, all)``: distinct material-plants with a recommendation generated in the period.

    *Evaluated* leaves out those whose only recommendation is NOT_EVALUABLE (a required input was missing, so
    nothing was computed) -- they were looked at but not assessed. Distinct, so a material-plant with several
    recommendations in the period counts once; this is deliberately not the number of records.
    """
    base = select(Recommendation.sap_material_number, Recommendation.sap_plant_code).where(
        *_in_period(period_start, exclusive_end)
    )
    every = count_of(session, base.distinct())
    evaluated = count_of(session, base.where(Recommendation.status != "NOT_EVALUABLE").distinct())
    return evaluated, every


def awaiting_decision(session: Session, period_start: date, exclusive_end: date) -> int:
    """Records generated in the period that are waiting on a decision *now*: a status as of the report, not a
    count of approval events."""
    return count_of(
        session,
        select(Recommendation.id).where(
            *_in_period(period_start, exclusive_end), Recommendation.status.in_(AWAITING_DECISION)
        ),
    )


def status_counts(session: Session, period_start: date, exclusive_end: date) -> dict[str, int]:
    """The current status of every record generated in the period."""
    rows = session.execute(
        select(Recommendation.status, func.count())
        .where(*_in_period(period_start, exclusive_end))
        .group_by(Recommendation.status)
    ).all()
    return {status: count for status, count in rows}


# --- Position: the newest recommendation of each material-plant -------------------------------------


def _population(period_start: date | None, exclusive_end: date, plant: str | None = None) -> Select:
    """The position population (one plant, or every plant). The smooth-demand rows the Overview leaves out are left
    out here too."""
    return _apply_filters(
        select(Recommendation),
        status=None,
        plant=plant,
        material=None,
        demand_class=None,
        is_oar=None,
        confidence=None,
        criticality=None,
        generated_from=None,
        generated_to=None,
    ).where(Recommendation.id.in_(newest_in_period_ids(period_start, exclusive_end)), _not_smooth())


def stockout_position(session: Session, base: Select, cutoff: date, plant: str | None = None) -> StockoutPosition:
    """The stockout counts over ``base``, marked stale when the usage was built too far from ``cutoff``."""
    counts = stockout_risk_counts(session, base, plant=plant)
    if counts.as_of is None:
        return StockoutPosition(
            at_risk=0,
            evaluated=0,
            insufficient_data=0,
            as_of=None,
            stale=False,
            stale_note="The stockout usage has not been built, so nothing can be judged.",
        )
    age = (cutoff - counts.as_of).days
    stale = abs(age) > MAX_MEASUREMENT_AGE_DAYS
    note = None
    if stale:
        note = (
            f"Stockout risk was last measured on {counts.as_of:%d %b %Y}, "
            f"{abs(age)} days {'before' if age > 0 else 'after'} this report's cutoff ({cutoff:%d %b %Y}); the "
            "consumption behind it is not current for the period, so it is not shown as the period's position."
        )
    return StockoutPosition(
        at_risk=counts.at_risk,
        evaluated=counts.evaluated,
        insufficient_data=counts.insufficient_data,
        as_of=counts.as_of,
        stale=stale,
        stale_note=note,
    )


def position_figures(
    session: Session, period_start: date | None, exclusive_end: date, cutoff: date, plant: str | None = None
) -> PositionFigures:
    """Stockout, excess and working capital over the newest recommendation of each material-plant in the period
    (for one plant, or every plant)."""
    base = _population(period_start, exclusive_end, plant)
    population = count_of(session, base)

    excess_base = base.where(
        Recommendation.current_max_stock.isnot(None),
        Recommendation.recommended_max_stock.isnot(None),
        Recommendation.current_max_stock > Recommendation.recommended_max_stock,
    )
    excess_count = count_of(session, excess_base)
    excess_opportunity = session.execute(
        excess_base.with_only_columns(
            func.sum(Recommendation.unit_price * (Recommendation.current_max_stock - Recommendation.recommended_max_stock))
        ).where(Recommendation.unit_price.isnot(None))
    ).scalar()

    priced = base.where(
        Recommendation.unit_price.isnot(None),
        Recommendation.current_rop.isnot(None),
        Recommendation.recommended_rop.isnot(None),
    )
    net_rop = session.execute(
        priced.with_only_columns(
            func.sum(Recommendation.unit_price * (Recommendation.current_rop - Recommendation.recommended_rop))
        )
    ).scalar()

    return PositionFigures(
        population=population,
        stockout=stockout_position(session, base, cutoff, plant),
        excess_count=excess_count,
        excess_opportunity=Decimal(excess_opportunity) if excess_opportunity is not None else None,
        net_rop_value_impact=Decimal(net_rop) if net_rop is not None else None,
        priced_count=count_of(session, priced),
        currency=_portfolio_currency(session),
    )



# --- Current vs recommended stock levels -------------------------------------------------------------


def stock_levels(session: Session, period_start: date | None, exclusive_end: date) -> list[dict]:
    """The mean current (SAP) and recommended safety stock, reorder point and max stock over the position population
    (the newest recommendation of each material-plant), each over the rows that have *both* values.

    A mean, labelled as one, with how many rows it is over. Sums divided here, so the mean is an exact Decimal on every
    database. A level with no row that has both values is ``None``, never 0.
    """
    base = _population(period_start, exclusive_end)
    pairs = {
        "safety_stock": (Recommendation.current_safety_stock, Recommendation.recommended_safety_stock),
        "reorder_point": (Recommendation.current_rop, Recommendation.recommended_rop),
        "max_stock": (Recommendation.current_max_stock, Recommendation.recommended_max_stock),
    }
    levels = []
    for key, (current, recommended) in pairs.items():
        total_current, total_recommended, rows = session.execute(
            base.with_only_columns(func.sum(current), func.sum(recommended), func.count()).where(
                current.isnot(None), recommended.isnot(None)
            )
        ).one()
        n = int(rows)
        levels.append(
            {
                "key": key,
                "aggregation": "mean",
                "records": n,
                "current": Decimal(str(total_current)) / n if n else None,
                "recommended": Decimal(str(total_recommended)) / n if n else None,
            }
        )
    return levels

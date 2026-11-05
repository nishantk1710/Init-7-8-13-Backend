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
from typing import NamedTuple

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.api.i7.recommendations import _apply_filters, _latest_only, _not_smooth, _portfolio_currency
from app.initiatives.i7.kpi_formulas.inventory_value import inventory_measurements, stock_date_problem, stock_loaded_at
from app.initiatives.i7.kpi_formulas.resolved import Formulas, load_active
from app.initiatives.i7.kpi_formulas.sql import excess_condition_sql, excess_value_sql
from app.initiatives.i7.kpi_formulas.working_capital import ELIGIBLE, NOT_ELIGIBLE, oar_rule, working_capital_impact
from app.initiatives.i7.stockout_risk.counts import stockout_risk_counts
from app.models.i7_recommendation import Recommendation, SapAdoptionResult
from app.schemas.i7.reports import PositionFigures, StockoutPosition

#: How far from the report's cutoff a stockout measurement can be and still speak for the period.
MAX_MEASUREMENT_AGE_DAYS = 31

#: The statuses in which a recommendation is waiting on someone's decision (the Overview's own awaiting-approval set).
AWAITING_DECISION = ("PENDING_APPROVAL", "HELD")

#: The statuses of a recommendation that has been approved: approved and awaiting SAP execution, and those taken on
#: to SAP afterwards (the Recommendations table's own "Approved" and "Implemented" groups).
APPROVED_STATUSES = ("APPROVED", "SAP_EXECUTION_PENDING", "SAP_EXECUTED", "ADOPTED", "PARTIALLY_ADOPTED", "NOT_ADOPTED")


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


#: The approval and SAP stages a recommendation's current status falls in. Exclusive; ``AWAITING_DECISION`` and
#: ``APPROVED_STATUSES`` are these stages' own sets, so the cards and the stages cannot disagree.
STAGE_STATUSES: dict[str, tuple[str, ...]] = {
    "not_submitted": ("READY_FOR_REVIEW", "NOT_EVALUABLE"),
    "awaiting_approval": AWAITING_DECISION,
    "adjusted_in_chain": ("ADJUSTED",),
    "approved_awaiting_sap": ("APPROVED", "SAP_EXECUTION_PENDING"),
    "implemented_in_sap": ("SAP_EXECUTED", "ADOPTED", "PARTIALLY_ADOPTED", "NOT_ADOPTED"),
    "rejected_or_sent_back": ("REJECTED", "SENT_BACK"),
}


def stage_counts(counts: dict[str, int]) -> dict[str, int]:
    """The status counts of the period's records grouped into the stages, plus ``total`` and ``other``."""
    stages = {stage: sum(counts.get(s, 0) for s in statuses) for stage, statuses in STAGE_STATUSES.items()}
    total = sum(counts.values())
    return {**stages, "total": total, "other": total - sum(stages.values())}


def eligible_oar_material_plants(session: Session, period_start: date, exclusive_end: date) -> int:
    """Eligible OAR material-plants in the period: the newest recommendation of each, with a recommended value, that the OAR rule
    counts (eligible and not rejected, or not eligible but with a live conversion request). One per material-plant, so a material
    recalculated several times counts once -- the same population as the Min-Max card and the Current vs recommended table."""
    statement = _latest_only(
        _apply_filters(
            select(Recommendation.id), status=None, plant=None, material=None, demand_class=None, is_oar=None, confidence=None,
            criticality=None, generated_from=period_start, generated_to=exclusive_end, demand_type="oar", has_recommended=True,
        ).where(_not_smooth())
    ).where(Recommendation.conversion_eligibility.in_((ELIGIBLE, NOT_ELIGIBLE)), oar_rule())
    return count_of(session, statement)


def oar_records(session: Session, period_start: date, exclusive_end: date) -> int:
    """Recommendation records generated in the period for *eligible* OAR material-plants -- rows, not distinct materials.

    The same OAR rule the Overview's Working Capital Impact uses (:func:`~app.initiatives.i7.kpi_formulas.working_capital.oar_rule`):
    an ELIGIBLE record counts unless it was rejected; a NOT_ELIGIBLE record counts only once somebody has asked to convert it and the
    request is alive (submitted for approval and not rejected). A NOT_ELIGIBLE record with no request, or a rejected one, is left out,
    and an OAR record with no eligibility verdict at all is not counted."""
    return count_of(
        session,
        select(Recommendation.id).where(
            *_in_period(period_start, exclusive_end),
            Recommendation.is_oar,
            Recommendation.conversion_eligibility.in_((ELIGIBLE, NOT_ELIGIBLE)),
            oar_rule(),
        ),
    )


class MinMaxCounts(NamedTuple):
    eligible: int
    """OAR material-plants the conversion rule says may convert to Min-Max ordering *and* that have a recommended value -- the same
    rule SAP adoption uses (an eligible OAR counts only when a value was calculated), so the two never disagree."""
    without_values: int
    """Eligible by the rule but with no recommended value at all: nothing to move to Min-Max yet. Counted apart, never in ``eligible``."""
    oar_material_plants: int
    manually_requested: int
    """NOT_ELIGIBLE ones a user asked to convert anyway; never added into ``eligible``."""
    converted: int
    """Eligible ones SAP now matches (the reconciliation's ADOPTED result): actually converted, not just approved."""
    with_evidence: int
    """Eligible ones the SAP reconciliation has any result for (adopted, partially or not adopted). With none, ``converted`` of 0 is
    "not measured yet", not a measured 0."""


def min_max_conversion(session: Session, period_start: date, exclusive_end: date) -> MinMaxCounts:
    """Min-Max conversion over the OAR recommendations the Recommendations list shows for the period: generated in it, and the newest of
    their material-plant. Built from the list's own filters, so every count here and the list behind it can never disagree.

    *Eligible* is the OAR rule's own verdict (``conversion_eligibility`` = ELIGIBLE) that the material-plant may convert, for those with
    at least one recommended value: a material-plant the rule allows but that has no value calculated has nothing to move, and is counted in
    ``without_values`` instead. Not a change made in SAP. *Converted* is the eligible ones SAP now matches (SAP adoption evidence): the
    change was actually made.
    """
    def oar_list(**more):
        return _latest_only(
            _apply_filters(
                select(Recommendation.id), status=None, plant=None, material=None, demand_class=None, is_oar=None, confidence=None,
                criticality=None, generated_from=period_start, generated_to=exclusive_end, demand_type="oar", **more,
            ).where(_not_smooth())
        )

    oar = oar_list()
    by_rule = oar.where(Recommendation.conversion_eligibility == "ELIGIBLE")
    eligible = oar_list(has_recommended=True).where(Recommendation.conversion_eligibility == "ELIGIBLE")
    evidence = select(SapAdoptionResult.recommendation_id).where(
        SapAdoptionResult.status.in_(("ADOPTED", "PARTIALLY_ADOPTED", "NOT_ADOPTED"))
    )
    eligible_count = count_of(session, eligible)
    return MinMaxCounts(
        eligible=eligible_count,
        without_values=count_of(session, by_rule) - eligible_count,
        oar_material_plants=count_of(session, oar),
        manually_requested=count_of(session, oar.where(Recommendation.manually_converted)),
        converted=count_of(
            session, oar_list(adopted_in_sap=True, has_recommended=True).where(Recommendation.conversion_eligibility == "ELIGIBLE")
        ),
        with_evidence=count_of(session, eligible.where(Recommendation.recommendation_id.in_(evidence))),
    )


def approved_count(session: Session, period_start: date, exclusive_end: date) -> int:
    """Records generated in the period that have been approved, as of the report: a status, not a count of approval
    events."""
    return count_of(
        session,
        select(Recommendation.id).where(
            *_in_period(period_start, exclusive_end), Recommendation.status.in_(APPROVED_STATUSES)
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


def stockout_position(
    session: Session, base: Select, cutoff: date, plant: str | None = None, formulas: Formulas | None = None
) -> StockoutPosition:
    """The stockout counts over ``base``, marked stale when the usage was built too far from ``cutoff``."""
    counts = stockout_risk_counts(session, base, plant=plant, formulas=formulas)
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

    # The Overview's own formulas (the saved version, else the defaults), so the report and the Overview
    # cannot disagree about a material-plant. The report stores the figures, not the formulas, so a later
    # edit never rewrites a report already generated.
    formulas = load_active(session)
    excess_base = base.where(excess_condition_sql(formulas))
    excess_count = count_of(session, excess_base)
    excess_value = excess_value_sql(formulas)
    excess_opportunity = session.execute(
        excess_base.with_only_columns(func.sum(excess_value)).where(excess_value.is_not(None))
    ).scalar()

    # The same calculation as the Overview card (kpi_formulas.working_capital): MBEW price and currency in ZAR.
    # In the report, working capital is the impact of what SAP has actually adopted -- not the estimate if everything were adopted.
    capital = working_capital_impact(session, base, formulas, adopted_only=True)
    capital_stock = working_capital_impact(session, base, formulas, segment="stock", adopted_only=True)
    capital_oar = working_capital_impact(session, base, formulas, segment="oar", adopted_only=True)
    # What it would be if every eligible material-plant were adopted: the Overview's calculation, adopted or not.
    capital_potential = working_capital_impact(session, base, formulas)
    capital_oar_potential = working_capital_impact(session, base, formulas, segment="oar")

    # Stock held, over the same population. Valued only when the stock load is close enough to the cutoff to speak for
    # it -- the same age rule the stockout reading answers to -- otherwise it is "not measured", never 0.
    loaded_at = stock_loaded_at(session)
    stock_problem = stock_date_problem(loaded_at, cutoff, MAX_MEASUREMENT_AGE_DAYS)
    held = inventory_measurements(session, base) if stock_problem is None else None
    newest = session.execute(select(func.max(Recommendation.generated_at)).where(
        Recommendation.id.in_(newest_in_period_ids(period_start, exclusive_end)))).scalar()

    return PositionFigures(
        population=population,
        stockout=stockout_position(session, base, cutoff, plant, formulas),
        excess_count=excess_count,
        excess_opportunity=Decimal(excess_opportunity) if excess_opportunity is not None else None,
        net_rop_value_impact=capital.amount,
        priced_count=capital.included_count,
        currency=_portfolio_currency(session),
        working_capital_currency=capital.currency,
        working_capital_excluded_count=capital.excluded_count,
        working_capital_split=True,
        working_capital_stock=capital_stock.amount,
        working_capital_stock_count=capital_stock.included_count,
        working_capital_oar=capital_oar.amount,
        working_capital_oar_count=capital_oar.included_count,
        working_capital_potential=capital_potential.amount,
        working_capital_potential_count=capital_potential.included_count,
        working_capital_oar_potential=capital_oar_potential.amount,
        working_capital_oar_potential_count=capital_oar_potential.included_count,
        inventory_value=held.inventory_value if held else None,
        inventory_priced_count=held.inventory_priced_count if held else 0,
        safety_stock_value=held.safety_stock_value if held else None,
        safety_stock_priced_count=held.safety_stock_priced_count if held else 0,
        below_reorder_count=held.below_reorder_count if held else None,
        below_reorder_judged=held.below_reorder_judged if held else 0,
        inventory_stock_as_of=loaded_at.date() if loaded_at else None,
        inventory_unavailable_reason=stock_problem,
        recommendation_reference_date=newest.date() if newest else None,
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

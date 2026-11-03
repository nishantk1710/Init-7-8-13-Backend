"""Assembles the management view of a report's period (``QuarterManagement``).

This is the only place the KPI cards are built. It calls the definitions in :mod:`kpi_definitions`, the monthly
table in :mod:`monthly_activity`, the forecast in :mod:`forecast_actual` and the coverage in :mod:`data_coverage`,
and writes the result into the saved report, so the page, the PDF and the Excel export all read one set of figures.

**A card is available or it says why not.** Nothing is replaced with zero, and no formula is invented to fill a
card: a figure that needs data the database does not have (a stockout measurement near the period, a price and both
reorder points) is NOT_AVAILABLE with the reason in plain words.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.initiatives.i7.kpi_formulas.expression import evaluate
from app.initiatives.i7.kpi_formulas.resolved import load_active
from app.initiatives.i7.reporting import kpi_definitions
from app.initiatives.i7.reporting.data_coverage import BASIS_CURRENT, build_data_coverage, coverage_items
from app.initiatives.i7.reporting.forecast_actual import build_forecast_actual
from app.initiatives.i7.reporting.monthly_activity import build_monthly_activity, period_months
from app.initiatives.i7.reporting.performance_trends import build_performance_trends
from app.initiatives.i7.reporting.quarter_trends import REALISED_SAVINGS_NOTE, build_trends
from app.initiatives.i7.reporting.period import (
    exclusive_end_of,
    is_period_complete,
    month_label,
    reporting_cutoff,
)
from app.api.i7.recommendations import _apply_filters
from app.core.config import get_settings
from app.models.i7_recommendation import Recommendation, SapAdoptionResult
from app.schemas.i7.reports import (
    AdoptionQuarter,
    AvailabilityStatus,
    ForecastActualSection,
    KpiCard,
    MetricKind,
    PeriodInfo,
    PeriodStatus,
    PlantPosition,
    PositionFigures,
    QuarterManagement,
    SourceActivitySection,
    StatusCount,
    StatusDistribution,
    WorkflowStages,
)

DEFINITIONS = [
    "Event in the period: something that happened during it, counted by when it happened (recommendations generated, "
    "approval decisions). It adds up across the months.",
    "Status as of the cutoff: how things stand on a date (recommendations awaiting a decision now). It is not an event "
    "and does not add up across months.",
    "Measurement on a date: a reading taken on one day (stockout risk, as of the last usage build). It is shown with its "
    "date and is never added across months.",
    "Estimate: working capital impact and excess inventory opportunity are what the recommendations would change if "
    "adopted. They are estimates, not savings that have been realised.",
]

MONEY_CAVEAT = (
    "Prices and quantities come from SAP extracts affected by a known number-format issue, so treat amounts as "
    "indicative until it is corrected."
)


def _period_label(period_start: date, period_end: date) -> str:
    start, end = month_label(period_start), month_label(period_end)
    return start if start == end else f"{start} – {end}"


def _unit(currency: str | None) -> str:
    return currency or "amount (currency not stated)"


def _adoption(
    session: Session, period_start: date, exclusive_end: date, awaiting: int, stages: WorkflowStages
) -> AdoptionQuarter:
    """Adoption of the period's recommendations that have something to adopt (the Overview's ``actionable`` rule,
    over the newest recommendation of each material-plant). Approval is not adoption: only SAP change evidence in the
    ledger (``i7_sap_adoption``) counts, and everything with no evidence is pending, never "not adopted". That pending
    is about SAP evidence, whether or not the recommendation was approved: where each recommendation stands in the
    approval chain is ``stages``, kept apart."""
    tracked = _apply_filters(
        select(Recommendation.recommendation_id),
        status=None, plant=None, material=None, demand_class=None, is_oar=None, confidence=None, criticality=None,
        generated_from=None, generated_to=None, actionable=True,
    ).where(Recommendation.id.in_(kpi_definitions.newest_in_period_ids(period_start, exclusive_end)))
    total = kpi_definitions.count_of(session, tracked)
    ledger = dict(
        session.execute(
            select(SapAdoptionResult.status, func.count())
            .where(SapAdoptionResult.recommendation_id.in_(tracked))
            .group_by(SapAdoptionResult.status)
        ).all()
    )
    adopted = int(ledger.get("ADOPTED", 0))
    partially = int(ledger.get("PARTIALLY_ADOPTED", 0))
    not_adopted = int(ledger.get("NOT_ADOPTED", 0))
    evidence = adopted + partially + not_adopted
    counts = dict(
        tracked=total, adopted=adopted, partially_adopted=partially, not_adopted=not_adopted,
        pending=max(total - evidence, 0), stages=stages,
        adoption_rate=_adoption_rate(session, adopted, partially, not_adopted, total - evidence, total),
    )
    if evidence == 0:
        return AdoptionQuarter(
            status=AvailabilityStatus.UNKNOWN,
            confirmed_adoptions=None,
            pending_approvals=awaiting,
            reason=(
                "Not measured: no SAP change evidence has been recorded for this period's recommendations, so adoption "
                "cannot be confirmed. An approval is not an adoption; this is distinct from a measured 0%."
            ),
            **counts,
        )
    return AdoptionQuarter(
        status=AvailabilityStatus.AVAILABLE,
        confirmed_adoptions=adopted,
        pending_approvals=awaiting,
        reason=(
            f"{evidence} of {total} recommendations with a change to adopt have SAP change evidence recorded; the rest "
            "have none yet. Confirmed adoptions are those whose SAP change matches what was approved."
        ),
        **counts,
    )


def _adoption_rate(session: Session, adopted: int, partially: int, not_adopted: int, pending: int, tracked: int) -> Decimal | None:
    """The Overview's own adoption rate: the formula in force on the Calculation page (by default adopted and partially
    adopted out of everything SAP has evidence for), over this period's counts. ``None`` -- never 0 -- when it cannot be
    worked out, as with nothing yet measured."""
    figures = {
        "adopted": Decimal(adopted), "partially_adopted": Decimal(partially), "not_adopted": Decimal(not_adopted),
        "pending": Decimal(max(pending, 0)), "tracked": Decimal(tracked),
    }
    rate = evaluate(load_active(session).parsed["adoption_rate"], figures)
    return rate if isinstance(rate, Decimal) else None


def _cards(
    *,
    from_snapshot: bool,
    quarter_end_label: str,
    quarter_end_reason: str | None,
    position_measured_on: date | None,
    period_label: str,
    cutoff: date,
    as_of: date,
    generated: int,
    assessed: int,
    every: int,
    awaiting: int,
    approved: int,
    oar_records: int,
    min_max: tuple[int, int, int],
    position: PositionFigures,
) -> list[KpiCard]:
    stockout = position.stockout
    # Where the position figures come from, said on the cards that use them. A valid quarter-end snapshot is a
    # measurement on the month's end; otherwise they are worked out now, and the card says so and why.
    basis = "snapshot" if from_snapshot else "current_calculation"
    position_note = (
        None if from_snapshot
        else (
            f"Current calculation, not a quarter-end measurement: no valid {quarter_end_label} snapshot ({quarter_end_reason})"
            if quarter_end_reason else None
        )
    )
    # The SAP values the excess and working-capital figures compare against are the ones stored on each recommendation
    # when it was generated, not a fresh read of SAP: say which date that is.
    reference_note = (
        f"Compares the SAP values stored on each recommendation when it was generated (newest "
        f"{position.recommendation_reference_date:%d %b %Y}) with the recommended ones; SAP is not read again for this report."
        if position.recommendation_reference_date
        else None
    )
    cards: list[KpiCard] = [
        KpiCard(
            key="materials_assessed",
            title="Material-plants with a computed recommendation",
            unit="material-plants",
            value=Decimal(assessed),
            status=AvailabilityStatus.AVAILABLE,
            explanation=(
                "Different material-plants for which a recommendation was actually worked out in this period. Each counts "
                "once, however many times it was regenerated."
            ),
            kind=MetricKind.EVENT,
            measured_on=None,
            period_label=period_label,
            denominator=every,
            denominator_label="distinct material-plants looked at",
            note=(
                f"{every - assessed:,} of the {every:,} material-plants looked at had no recommendation computed because a "
                "required input was missing. They are looked at, not assessed, and are not counted here."
            ),
        ),
        KpiCard(
            key="recommendations_generated",
            title="Recommendations generated",
            unit="recommendation records",
            value=Decimal(generated),
            status=AvailabilityStatus.AVAILABLE,
            explanation=(
                "Recommendation records created in this period. These are records, not materials: a material-plant that "
                "was recalculated has a record for each run."
            ),
            kind=MetricKind.EVENT,
            measured_on=None,
            period_label=period_label,
            note=(
                f"{generated:,} records over {every:,} distinct material-plants: "
                f"{generated - every:,} are repeat runs of a material-plant already recommended in the period."
            ),
        ),
    ]

    # --- High Stockout Risk: a measurement on the usage build date ---------------------------------
    if stockout.as_of is None or stockout.stale:
        cards.append(
            KpiCard(
                key="critical_stockout_risk",
                title="High Stockout Risk",
                unit="material-plants",
                value=None,
                status=AvailabilityStatus.NOT_AVAILABLE,
                explanation="Fast and slow moving material-plants whose stock on hand is below what their lead time needs.",
                kind=MetricKind.MEASUREMENT,
                measured_on=stockout.as_of,
                period_label=f"as of {stockout.as_of:%d %b %Y}" if stockout.as_of else "not measured",
                basis=basis,
                unavailable_reason=stockout.stale_note,
            )
        )
    else:
        cards.append(
            KpiCard(
                key="critical_stockout_risk",
                title="High Stockout Risk",
                unit="material-plants",
                value=Decimal(stockout.at_risk),
                status=AvailabilityStatus.AVAILABLE,
                explanation="Fast and slow moving material-plants whose stock on hand is below what their lead time needs.",
                kind=MetricKind.MEASUREMENT,
                measured_on=stockout.as_of,
                period_label=f"as of {stockout.as_of:%d %b %Y}",
                basis=basis,
                denominator=stockout.evaluated,
                denominator_label="material-plants that could be judged",
                note=(
                    f"{stockout.insufficient_data} more could not be judged for want of a lead time, stock or "
                    "consumption figure; they are never counted as safe."
                    if stockout.insufficient_data
                    else None
                ),
            )
        )

    # --- Excess inventory opportunity (estimate) ---------------------------------------------------
    cards.append(
        KpiCard(
            key="excess_inventory_opportunity",
            title="Excess inventory opportunity",
            unit=_unit(position.currency),
            value=position.excess_opportunity,
            status=AvailabilityStatus.AVAILABLE if position.excess_opportunity is not None else AvailabilityStatus.NOT_AVAILABLE,
            explanation=(
                "Estimated value of stock above the recommended maximum, over the "
                f"{position.excess_count} material-plants whose current maximum is above it. An estimate, not a saving."
            ),
            kind=MetricKind.MEASUREMENT,
            measured_on=position_measured_on if from_snapshot else cutoff,
            period_label=f"{quarter_end_label} month-end" if from_snapshot else period_label,
            denominator=position.population,
            denominator_label="newest recommendation of each material-plant",
            basis=basis,
            unavailable_reason=(
                None if position.excess_opportunity is not None else "No excess material-plant has a unit price, a current and a recommended maximum stock together."
            ),
            note=" ".join(n for n in (position_note, reference_note, MONEY_CAVEAT) if n),
        )
    )

    # --- Working capital impact (estimate, on the reorder point) ----------------------------------
    cards.append(
        KpiCard(
            key="working_capital_impact",
            title="Working capital impact",
            unit=_unit(position.working_capital_currency or position.currency),
            value=position.net_rop_value_impact,
            status=AvailabilityStatus.AVAILABLE if position.net_rop_value_impact is not None else AvailabilityStatus.NOT_AVAILABLE,
            explanation=(
                "Estimated change in cash tied up in stock if every recommended reorder point were adopted: negative "
                "means more tied up, positive means released. An estimate, not a realised saving. In ZAR, from each "
                "material's SAP valuation price (ZAR, and NAD at 1:1): "
                f"{position.priced_count} material-plants included, {position.working_capital_excluded_count} left out "
                "(another currency, no stated currency, no valid price or no reorder point)."
            ),
            kind=MetricKind.MEASUREMENT,
            measured_on=position_measured_on if from_snapshot else cutoff,
            period_label=f"{quarter_end_label} month-end" if from_snapshot else period_label,
            denominator=position.priced_count,
            denominator_label="material-plants included (ZAR/NAD price and both reorder points)",
            basis=basis,
            unavailable_reason=(
                None if position.net_rop_value_impact is not None else "No recommendation has a ZAR or NAD unit price and both a current and a recommended reorder point."
            ),
            note=" ".join(n for n in (position_note, reference_note, MONEY_CAVEAT) if n),
        )
    )

    # --- Approval: two statuses as of the date the report was generated -----------------------------
    # Both are where the period's recommendations stand in the approval chain on that date. Neither says anything about
    # SAP: whether SAP was updated is the adoption section, a separate process.
    cards.append(
        KpiCard(
            key="pending_approval",
            title="Pending approval",
            unit="recommendations",
            value=Decimal(awaiting),
            status=AvailabilityStatus.AVAILABLE,
            explanation=(
                "Recommendations from this period waiting for an approver's decision (pending or on hold) on the date this "
                "report was generated. Not the same as waiting for SAP to be updated."
            ),
            kind=MetricKind.STATUS_AS_OF,
            measured_on=as_of,
            period_label=f"as of {as_of:%d %b %Y}",
            denominator=generated,
            denominator_label="recommendations generated in the period",
        )
    )
    cards.append(
        KpiCard(
            key="approved",
            title="Approved",
            unit="recommendations",
            value=Decimal(approved),
            status=AvailabilityStatus.AVAILABLE,
            explanation=(
                "Recommendations generated in this period that are in an approved status (approved, or approved and "
                "since acted on in SAP) on the date this report was generated. Approved is not adopted: whether SAP "
                "changed is the adoption section."
            ),
            kind=MetricKind.STATUS_AS_OF,
            measured_on=as_of,
            period_label=f"as of {as_of:%d %b %Y}",
            denominator=generated,
            denominator_label="recommendations generated in the period",
        )
    )
    # --- OAR: how many records, and how many material-plants may convert to Min-Max ordering ------------
    eligible, oar_material_plants, manual = min_max
    cards.append(
        KpiCard(
            key="oar_recommendations",
            title="OAR recommendations",
            unit="recommendation records",
            value=Decimal(oar_records),
            status=AvailabilityStatus.AVAILABLE,
            explanation=(
                "Recommendation records generated in this period for OAR material-plants, whose levels are sized from similar "
                "materials. These are records, not materials: an OAR material-plant that was recalculated has a record for each run."
            ),
            kind=MetricKind.EVENT,
            measured_on=None,
            period_label=period_label,
            denominator=generated,
            denominator_label="recommendations generated in the period",
        )
    )
    cards.append(
        KpiCard(
            key="min_max_conversion",
            title="Min-Max conversion",
            unit="material-plants",
            value=Decimal(eligible),
            status=AvailabilityStatus.AVAILABLE,
            explanation=(
                "OAR material-plants whose newest recommendation from this period is eligible to be converted to Min-Max ordering, by the "
                "conversion rule. Eligible is the rule's verdict, not a change made in SAP: whether SAP was changed is the SAP adoption "
                "section."
            ),
            kind=MetricKind.STATUS_AS_OF,
            measured_on=as_of,
            period_label=f"as of {as_of:%d %b %Y}",
            denominator=oar_material_plants,
            denominator_label="OAR material-plants",
            note=(
                f"{manual:,} more were not eligible but were requested for conversion by a user; they are not counted here."
                if manual
                else None
            ),
        )
    )
    return cards


def _coverage(
    session: Session, period_start: date, period_end: date, today: date, cutoff: date, *,
    source_activity: SourceActivitySection | None, position: PositionFigures, trends, from_snapshot: bool,
    forecast: ForecastActualSection,
):
    """The data-coverage section, with the basis said honestly: which figures are a saved month-end snapshot and which are
    worked out now, and which months have no snapshot."""
    coverage = build_data_coverage(
        session, period_start, period_end, today, cutoff, source_activity=source_activity, position=position
    )
    warnings = [w for w in coverage.warnings if w != BASIS_CURRENT]
    warnings += [n for n in trends.notes if n != REALISED_SAVINGS_NOTE]  # which months have no snapshot, or why none can be read
    if from_snapshot:
        end = trends.quarter_end
        warnings.append(
            f"The quarter-end position (stockout, excess, working capital) is the saved {end.label} month-end snapshot, "
            f"measured {end.measured_on:%d %b %Y}. Everything else is a current calculation."
        )
    else:
        warnings.append(BASIS_CURRENT)
        if trends.quarter_end.reason:
            warnings.append(f"No quarter-end snapshot: {trends.quarter_end.reason}")
    return coverage.model_copy(
        update={
            "basis": "snapshot" if from_snapshot else "current_calculation",
            "warnings": warnings,
            "items": coverage_items(coverage, position, trends.quarter_end, forecast),
        }
    )


def build_quarter_management(
    session: Session,
    period_start: date,
    period_end: date,
    *,
    today: date,
    source_activity: SourceActivitySection | None,
) -> QuarterManagement:
    exclusive_end = exclusive_end_of(period_end)
    cutoff = reporting_cutoff(period_end, today)
    complete = is_period_complete(period_end, today)
    period_label = _period_label(period_start, period_end)

    generated = kpi_definitions.records_generated(session, period_start, exclusive_end)
    assessed, every = kpi_definitions.material_plants_assessed(session, period_start, exclusive_end)
    awaiting = kpi_definitions.awaiting_decision(session, period_start, exclusive_end)
    approved = kpi_definitions.approved_count(session, period_start, exclusive_end)
    oar_records = kpi_definitions.oar_records(session, period_start, exclusive_end)
    min_max = kpi_definitions.min_max_conversion(session, period_start, exclusive_end)
    position = kpi_definitions.position_figures(session, period_start, exclusive_end, cutoff)
    # The Overview's by-plant charts: every plant, then each plant it offers. Narrowed to the plants this
    # deployment actually carries (I7_PLANTS), in the Overview's order.
    plants = [code for code in ("1300", "1500") if code in {p.strip() for p in get_settings().i7_plants.split(",")}]
    plant_positions = [PlantPosition(plant=None, figures=position)] + [
        PlantPosition(plant=code, figures=kpi_definitions.position_figures(session, period_start, exclusive_end, cutoff, code))
        for code in plants
    ]
    # Saved monthly snapshots: the trend, and -- only when the last month's is valid -- the quarter-end position, which then
    # replaces the current calculation above for the cards and the by-plant charts, so they all read one measurement.
    trends, snapshot_position, snapshot_plants = build_trends(session, period_start, period_end)
    from_snapshot = snapshot_position is not None
    if from_snapshot:
        position, plant_positions = snapshot_position, snapshot_plants
    forecast = build_forecast_actual(session, period_start, period_end)

    months = period_months(period_start, period_end, today, source_activity)
    usable = [m.label for m in months if m.usable]
    period = PeriodInfo(
        status=PeriodStatus.COMPLETE if complete else PeriodStatus.IN_PROGRESS,
        cutoff=cutoff,
        months=months,
        note=(
            f"{period_label} is complete."
            if complete
            else f"{period_label} is still in progress: figures run to {cutoff:%d %b %Y}. "
            + (f"Months with usable data: {', '.join(usable)}." if usable else "No month has usable consumption data yet.")
        ),
    )

    counts = kpi_definitions.status_counts(session, period_start, exclusive_end)
    total = sum(counts.values())
    distribution = StatusDistribution(
        basis=(
            "Current status of each recommendation generated in this period, as of the date this report was "
            "generated -- not where it stood at the end of the period, and not the approval events recorded during it."
        ),
        measured_on=today,
        total=total,
        rows=[
            StatusCount(
                status=status,
                count=count,
                percentage=(Decimal(count) / Decimal(total) * 100).quantize(Decimal("0.01")) if total else None,
            )
            for status, count in sorted(counts.items(), key=lambda item: -item[1])
        ],
    )

    return QuarterManagement(
        period=period,
        monthly_activity=build_monthly_activity(
            session, period_start, period_end, today, source_activity=source_activity, forecast=forecast
        ),
        kpis=_cards(
            from_snapshot=from_snapshot,
            quarter_end_label=trends.quarter_end.label,
            quarter_end_reason=trends.quarter_end.reason,
            position_measured_on=trends.quarter_end.measured_on,
            period_label=period_label,
            cutoff=cutoff,
            as_of=today,
            generated=generated,
            assessed=assessed,
            every=every,
            awaiting=awaiting,
            approved=approved,
            oar_records=oar_records,
            min_max=min_max,
            position=position,
        ),
        position=position,
        position_basis="snapshot" if from_snapshot else "current_calculation",
        trends=trends,
        performance=build_performance_trends(session, period_end, today),
        plant_positions=plant_positions,
        status_distribution=distribution,
        forecast_actual=forecast,
        adoption=_adoption(session, period_start, exclusive_end, awaiting, WorkflowStages(**kpi_definitions.stage_counts(counts))),
        data_coverage=_coverage(
            session, period_start, period_end, today, cutoff,
            source_activity=source_activity, position=position, trends=trends, from_snapshot=from_snapshot, forecast=forecast,
        ),
        definitions=DEFINITIONS,
    )

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

from app.initiatives.i7.reporting import kpi_definitions
from app.initiatives.i7.reporting.data_coverage import build_data_coverage
from app.initiatives.i7.reporting.forecast_actual import build_forecast_actual
from app.initiatives.i7.reporting.monthly_activity import build_monthly_activity, period_months
from app.initiatives.i7.reporting.period import (
    exclusive_end_of,
    is_period_complete,
    month_label,
    reporting_cutoff,
)
from app.core.config import get_settings
from app.models.i7_recommendation import Recommendation, SapAdoptionResult
from app.schemas.i7.reports import (
    AdoptionQuarter,
    AvailabilityStatus,
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
    session: Session, period_start: date, exclusive_end: date, awaiting: int
) -> AdoptionQuarter:
    """Adoption of the recommendations generated in the period. Approval is not adoption: only SAP change evidence
    in the ledger (``i7_sap_adoption``) counts."""
    ledger = dict(
        session.execute(
            select(SapAdoptionResult.status, func.count())
            .join(Recommendation, Recommendation.recommendation_id == SapAdoptionResult.recommendation_id)
            .where(Recommendation.generated_at >= period_start, Recommendation.generated_at < exclusive_end)
            .group_by(SapAdoptionResult.status)
        ).all()
    )
    evidence = sum(count for status, count in ledger.items() if status != "UNKNOWN")
    if evidence == 0:
        return AdoptionQuarter(
            status=AvailabilityStatus.UNKNOWN,
            confirmed_adoptions=None,
            pending_approvals=awaiting,
            reason=(
                "Not measured: no SAP change evidence has been recorded for this period's recommendations, so adoption "
                "cannot be confirmed. An approval is not an adoption; this is distinct from a measured 0%."
            ),
        )
    return AdoptionQuarter(
        status=AvailabilityStatus.AVAILABLE,
        confirmed_adoptions=int(ledger.get("ADOPTED", 0)),
        pending_approvals=awaiting,
        reason=(
            f"{evidence} recommendations have SAP change evidence recorded; the rest have none yet. Confirmed adoptions "
            "are those whose SAP change matches what was approved."
        ),
    )


def _cards(
    *,
    period_label: str,
    cutoff: date,
    as_of: date,
    generated: int,
    assessed: int,
    every: int,
    awaiting: int,
    position: PositionFigures,
) -> list[KpiCard]:
    stockout = position.stockout
    cards: list[KpiCard] = [
        KpiCard(
            key="materials_assessed",
            title="Materials assessed",
            unit="material-plants",
            value=Decimal(assessed),
            status=AvailabilityStatus.AVAILABLE,
            explanation="Different material-plants the system actually worked out a recommendation for in this period.",
            kind=MetricKind.EVENT,
            measured_on=None,
            period_label=period_label,
            denominator=every,
            denominator_label="material-plants with a recommendation",
            note="Distinct material-plants, not records: one with several recommendations counts once. Those with no "
            "computable recommendation are left out.",
        ),
        KpiCard(
            key="recommendations_generated",
            title="Recommendations generated",
            unit="recommendation records",
            value=Decimal(generated),
            status=AvailabilityStatus.AVAILABLE,
            explanation="Recommendation records created in this period.",
            kind=MetricKind.EVENT,
            measured_on=None,
            period_label=period_label,
        ),
    ]

    # --- Critical stockout risk: a measurement on the usage build date -----------------------------
    if stockout.as_of is None or stockout.stale:
        cards.append(
            KpiCard(
                key="critical_stockout_risk",
                title="Critical stockout risk",
                unit="material-plants",
                value=None,
                status=AvailabilityStatus.NOT_AVAILABLE,
                explanation="Fast and slow moving material-plants whose stock on hand is below what their lead time needs.",
                kind=MetricKind.MEASUREMENT,
                measured_on=stockout.as_of,
                period_label=f"as of {stockout.as_of:%d %b %Y}" if stockout.as_of else "not measured",
                unavailable_reason=stockout.stale_note,
            )
        )
    else:
        cards.append(
            KpiCard(
                key="critical_stockout_risk",
                title="Critical stockout risk",
                unit="material-plants",
                value=Decimal(stockout.at_risk),
                status=AvailabilityStatus.AVAILABLE,
                explanation="Fast and slow moving material-plants whose stock on hand is below what their lead time needs.",
                kind=MetricKind.MEASUREMENT,
                measured_on=stockout.as_of,
                period_label=f"as of {stockout.as_of:%d %b %Y}",
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
            measured_on=cutoff,
            period_label=period_label,
            denominator=position.population,
            denominator_label="newest recommendation of each material-plant",
            unavailable_reason=(
                None if position.excess_opportunity is not None else "No excess material-plant has a unit price, a current and a recommended maximum stock together."
            ),
            note=MONEY_CAVEAT,
        )
    )

    # --- Working capital impact (estimate, on the reorder point) ----------------------------------
    cards.append(
        KpiCard(
            key="working_capital_impact",
            title="Working capital impact",
            unit=_unit(position.currency),
            value=position.net_rop_value_impact,
            status=AvailabilityStatus.AVAILABLE if position.net_rop_value_impact is not None else AvailabilityStatus.NOT_AVAILABLE,
            explanation=(
                "Estimated change in cash tied up in stock if every recommended reorder point were adopted: negative "
                "means more tied up, positive means released. An estimate, not a realised saving."
            ),
            kind=MetricKind.MEASUREMENT,
            measured_on=cutoff,
            period_label=period_label,
            denominator=position.priced_count,
            denominator_label="recommendations with a price and both reorder points",
            unavailable_reason=(
                None if position.net_rop_value_impact is not None else "No recommendation has a unit price and both a current and a recommended reorder point."
            ),
            note=MONEY_CAVEAT,
        )
    )

    # --- Pending approval: a status as of the cutoff -----------------------------------------------
    cards.append(
        KpiCard(
            key="pending_approval",
            title="Pending approval",
            unit="recommendations",
            value=Decimal(awaiting),
            status=AvailabilityStatus.AVAILABLE,
            explanation=(
                "Recommendations from this period that are waiting for someone's decision on the date this report was "
                "generated. Where they stood at the end of the period is not what this shows."
            ),
            kind=MetricKind.STATUS_AS_OF,
            measured_on=as_of,
            period_label=f"as of {as_of:%d %b %Y}",
            denominator=generated,
            denominator_label="recommendations generated in the period",
        )
    )
    return cards


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
    position = kpi_definitions.position_figures(session, period_start, exclusive_end, cutoff)
    # The Overview's by-plant charts: every plant, then each plant it offers. Narrowed to the plants this
    # deployment actually carries (I7_PLANTS), in the Overview's order.
    plants = [code for code in ("1300", "1500") if code in {p.strip() for p in get_settings().i7_plants.split(",")}]
    plant_positions = [PlantPosition(plant=None, figures=position)] + [
        PlantPosition(plant=code, figures=kpi_definitions.position_figures(session, period_start, exclusive_end, cutoff, code))
        for code in plants
    ]
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
            period_label=period_label,
            cutoff=cutoff,
            as_of=today,
            generated=generated,
            assessed=assessed,
            every=every,
            awaiting=awaiting,
            position=position,
        ),
        position=position,
        plant_positions=plant_positions,
        status_distribution=distribution,
        forecast_actual=forecast,
        adoption=_adoption(session, period_start, exclusive_end, awaiting),
        data_coverage=build_data_coverage(
            session, period_start, period_end, today, cutoff, source_activity=source_activity, position=position
        ),
        definitions=DEFINITIONS,
    )

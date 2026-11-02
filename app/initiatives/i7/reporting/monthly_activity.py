"""What happened in each month of the report's period.

The table the management reader sees: ``Metric | Month 1 | Month 2 | Month 3 | Quarter total``.

**Every row is an EVENT or an AGGREGATE counted by when it happened**, never by a status today:

* *Recommendations generated* -- ``i7_recommendation.generated_at``.
* *Submitted for approval / approval decisions / rejections / sent back* -- dated rows of the append-only approval
  ledger (``i7_approval_ledger.timestamp``), by the ``action`` recorded, **not** by a recommendation's latest
  status. A recommendation approved in November and rejected in December is one approval in November and one
  rejection in December.
* *SAP consumption* -- staged goods-issue transactions in the month they happened in (``source_activity``), so a
  quarter in which I07 generated nothing still has its months.
* *Forecast and actual demand* -- the champion backtests' one-month-ahead sums, per target month.

**Duplicates.** A ledger event is identified by recommendation, version, actor, action and timestamp, and counted once
however many identical rows exist (a retried write must not count twice). The ledger is append-only, so this
only matters when something wrote the same event more than once.

**Not started, not covered, not tracked are not zero.** A month that has not started has no value. A month the staged
consumption does not cover has no value. SAP adoption is stored as the latest result per recommendation, not as dated
events, so it has no monthly series at all and says so. ``None`` is "not available"; ``0`` is a real count of nothing.

**The months add up.** For each additive row the months are summed into the quarter total, and that total is checked
against the same thing counted over the whole period in one query (``reconciles``). A mismatch means a record fell
outside the months' buckets, and the row says so rather than hiding it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from app.initiatives.i7.reporting import kpi_definitions
from app.initiatives.i7.reporting.data_coverage import generated_by_month
from app.initiatives.i7.reporting.period import exclusive_end_of, month_label, month_state, months_of
from app.models.i7_recommendation import ApprovalLedgerEntry
from app.schemas.i7.reports import (
    ActivityRow,
    AvailabilityStatus,
    ForecastActualSection,
    MetricKind,
    MonthlyActivity,
    PeriodMonth,
    SourceActivitySection,
    SourceMonthStatus,
)

#: Ledger action(s) -> (row key, row label, plain-language note). A RESUBMIT (a manual Min-Max conversion asked for
#: again after a rejection) is a send into the chain like a SUBMIT.
LEDGER_ROWS: tuple[tuple[str | tuple[str, ...], str, str, str], ...] = (
    (("SUBMIT", "RESUBMIT"), "submitted_for_approval", "Sent for approval", "Recommendations sent into the approval chain."),
    ("APPROVE", "approval_decisions", "Approval decisions", "One per role that approved at its step; a recommendation passes several."),
    ("REJECT", "rejections", "Rejections", "Rejected by an approver, or declined by the planner before it was sent."),
    ("SEND_BACK", "sent_back", "Sent back", "Returned a step for correction."),
)


def _sum(values: list[int | Decimal | None]) -> int | Decimal | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _month_values(counts: dict[tuple[int, int], int], months: list[date], states: list[str]) -> list[int | None]:
    """A count per month; ``None`` for a month that has not started (nothing can have happened yet)."""
    return [None if state == "not_started" else counts.get((m.year, m.month), 0) for m, state in zip(months, states)]


def _events_by_month(session: Session, action: str | tuple[str, ...], period_start: date, exclusive_end: date) -> dict[tuple[int, int], int]:
    events = (
        select(
            ApprovalLedgerEntry.recommendation_id,
            ApprovalLedgerEntry.recommendation_version,
            ApprovalLedgerEntry.actor_id,
            ApprovalLedgerEntry.action,
            ApprovalLedgerEntry.timestamp,
        )
        .where(
            ApprovalLedgerEntry.action.in_((action,) if isinstance(action, str) else action),
            ApprovalLedgerEntry.timestamp >= period_start,
            ApprovalLedgerEntry.timestamp < exclusive_end,
        )
        .distinct()
        .subquery()
    )
    rows = session.execute(
        select(extract("year", events.c.timestamp), extract("month", events.c.timestamp), func.count()).group_by(
            extract("year", events.c.timestamp), extract("month", events.c.timestamp)
        )
    ).all()
    return {(int(y), int(m)): int(n) for y, m, n in rows}


def _distinct_events_in_period(session: Session, action: str | tuple[str, ...], period_start: date, exclusive_end: date) -> int:
    events = (
        select(
            ApprovalLedgerEntry.recommendation_id,
            ApprovalLedgerEntry.recommendation_version,
            ApprovalLedgerEntry.actor_id,
            ApprovalLedgerEntry.action,
            ApprovalLedgerEntry.timestamp,
        )
        .where(
            ApprovalLedgerEntry.action.in_((action,) if isinstance(action, str) else action),
            ApprovalLedgerEntry.timestamp >= period_start,
            ApprovalLedgerEntry.timestamp < exclusive_end,
        )
        .distinct()
    )
    return kpi_definitions.count_of(session, events)


def period_months(
    period_start: date, period_end: date, today: date, source_activity: SourceActivitySection | None
) -> list[PeriodMonth]:
    """The period's months, with what has happened to each and whether the staged consumption covers it."""
    covered = {
        m.month: m.status in (SourceMonthStatus.AVAILABLE, SourceMonthStatus.NO_ACTIVITY)
        for m in (source_activity.months if source_activity else [])
    }
    months = []
    for m in months_of(period_start, period_end):
        state = month_state(m, period_end, today)
        has_data = covered.get(m, False)
        months.append(
            PeriodMonth(
                month=m,
                label=month_label(m),
                state=state,
                has_consumption_data=has_data,
                usable=state != "not_started" and has_data,
            )
        )
    return months


def build_monthly_activity(
    session: Session,
    period_start: date,
    period_end: date,
    today: date,
    *,
    source_activity: SourceActivitySection | None,
    forecast: ForecastActualSection,
) -> MonthlyActivity:
    exclusive_end = exclusive_end_of(period_end)
    months = months_of(period_start, period_end)
    states = [month_state(m, period_end, today) for m in months]
    rows: list[ActivityRow] = []

    # --- SAP consumption: goods-issue transactions, by the month they happened in -----------------
    consumption: list[int | None] = []
    by_month = {m.month: m for m in (source_activity.months if source_activity else [])}
    for m, state in zip(months, states):
        staged = by_month.get(m)
        usable = (
            state != "not_started"
            and staged is not None
            and staged.status in (SourceMonthStatus.AVAILABLE, SourceMonthStatus.NO_ACTIVITY)
        )
        consumption.append((staged.issue_transactions or 0) if usable and staged else None)
    consumption_total = _sum(consumption)
    rows.append(
        ActivityRow(
            key="sap_consumption_transactions",
            label="SAP consumption",
            unit="goods-issue transactions",
            kind=MetricKind.EVENT,
            months=consumption,
            quarter_total=consumption_total,
            status=AvailabilityStatus.AVAILABLE if consumption_total is not None else AvailabilityStatus.NOT_AVAILABLE,
            reconciles=(
                consumption_total == source_activity.totals.issue_transactions
                if source_activity and consumption_total is not None
                else None
            ),
            note=(
                "Counted as transactions, not quantity: the staged quantities are withheld until the SAP "
                "number-format correction is applied. A month the staged data does not cover is blank, not 0."
            ),
        )
    )

    # --- Recommendations generated ---------------------------------------------------------------
    generated = _month_values(generated_by_month(session, period_start, exclusive_end), months, states)
    generated_total = _sum(generated)
    rows.append(
        ActivityRow(
            key="recommendations_generated",
            label="Recommendations generated",
            unit="recommendation records",
            kind=MetricKind.EVENT,
            months=generated,
            quarter_total=generated_total,
            status=AvailabilityStatus.AVAILABLE if generated_total is not None else AvailabilityStatus.NOT_AVAILABLE,
            reconciles=(
                generated_total == kpi_definitions.records_generated(session, period_start, exclusive_end)
                if generated_total is not None
                else None
            ),
            note="Records, not distinct materials: a material-plant regenerated in the period has one record per run.",
        )
    )

    # --- Decisions, from dated approval events ---------------------------------------------------
    for action, key, label, note in LEDGER_ROWS:
        values = _month_values(_events_by_month(session, action, period_start, exclusive_end), months, states)
        total = _sum(values)
        rows.append(
            ActivityRow(
                key=key,
                label=label,
                unit="decisions",
                kind=MetricKind.EVENT,
                months=values,
                quarter_total=total,
                status=AvailabilityStatus.AVAILABLE if total is not None else AvailabilityStatus.NOT_AVAILABLE,
                reconciles=(
                    total == _distinct_events_in_period(session, action, period_start, exclusive_end)
                    if total is not None
                    else None
                ),
                note=f"{note} Counted from the dated approval history, not from a recommendation's current status.",
            )
        )

    # --- SAP adoption: not tracked as dated events -----------------------------------------------
    rows.append(
        ActivityRow(
            key="sap_adoptions_confirmed",
            label="Confirmed SAP adoptions",
            unit="adoptions",
            kind=MetricKind.EVENT,
            months=[None for _ in months],
            quarter_total=None,
            status=AvailabilityStatus.UNKNOWN,
            note=(
                "Not measured by month: adoption is stored as the latest result for each recommendation, not as dated "
                "events, so it cannot be placed in a month. See the quarter-level SAP adoption card."
            ),
        )
    )

    # --- Forecast and actual demand, by target month --------------------------------------------
    by_forecast = {p.month: p for p in forecast.months}
    for key, label, pick in (
        ("forecast_demand", "Forecast demand", lambda p: p.forecast),
        ("actual_demand", "Actual demand", lambda p: p.actual),
    ):
        values = [pick(by_forecast[m]) if m in by_forecast and states[i] != "not_started" else None for i, m in enumerate(months)]
        total = _sum(values)
        rows.append(
            ActivityRow(
                key=key,
                label=label,
                unit="units (each material's own unit of measure)",
                kind=MetricKind.AGGREGATE,
                months=values,
                quarter_total=total,
                status=AvailabilityStatus.AVAILABLE if total is not None else AvailabilityStatus.NOT_AVAILABLE,
                note=forecast.note,
            )
        )

    return MonthlyActivity(months=period_months(period_start, period_end, today, source_activity), rows=rows)

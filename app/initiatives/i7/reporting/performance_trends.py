"""The report's performance trends: the last few months of recommendation volume, approval activity and excess inventory.

Each series comes from the one source that can honestly say what happened in a past month, and none is rebuilt from today:

* *Recommendations generated* -- ``i7_recommendation.generated_at``, as records and as distinct material-plants (a
  material-plant regenerated in a month is one of the second and several of the first).
* *Submitted / approved / rejected / sent back* -- dated rows of the approval ledger, counted in the month they happened
  in, by the ``action`` recorded -- never by a recommendation's status today. Counted once however many identical rows
  exist, as in the monthly activity table.
* *Excess inventory* -- the saved month-end snapshot of that month. A month with no snapshot, a month still running and a
  snapshot taken too long after its month ended have no point: the stock behind excess is only known as it is now, so a
  past month cannot be rebuilt.

**No figure is not zero.** A month before a source's history began, or one that has not started, is ``None``, so a chart
breaks its line there instead of drawing 0. From the first month with any record, a month with none is a real 0 for the
two event series.

**SAP implementation has no series.** SAP adoption is stored as the latest result per recommendation, not as dated
events, so how many were implemented in each past month cannot be told. The trend says so; it does not infer it from
today's status.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from app.initiatives.i7.reporting import snapshots
from app.initiatives.i7.reporting.data_coverage import generated_by_month
from app.initiatives.i7.reporting.monthly_activity import LEDGER_ROWS, _events_by_month
from app.initiatives.i7.reporting.period import exclusive_end_of, month_label, month_state, months_of
from app.initiatives.i7.reporting.quarter_trends import _load
from app.models.i7_kpi_snapshot import STATUS_FINAL
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation
from app.schemas.i7.reports import PerformanceMonth, PerformanceTrends

#: How many months the trends reach back, the period's own month included.
TREND_MONTHS = 6

#: Fewer month-end excess points than this is not a trend, and the report says so.
MIN_TREND_POINTS = 2

IMPLEMENTATION_NOTE = (
    "SAP implementation has no monthly series: SAP adoption is saved as the latest result per recommendation, not as dated events, so "
    "how many were implemented in each past month cannot be told. It is not inferred from today's status."
)


def trend_months(period_end: date) -> list[date]:
    """The first day of each of the last ``TREND_MONTHS`` calendar months up to and including the period's last month."""
    index = period_end.year * 12 + (period_end.month - 1) - (TREND_MONTHS - 1)
    first = date(index // 12, index % 12 + 1, 1)
    return months_of(first, period_end)


def _material_plants_by_month(session: Session, period_start: date, exclusive_end: date) -> dict[tuple[int, int], int]:
    """Distinct material-plants with a recommendation generated in each month."""
    year, month = extract("year", Recommendation.generated_at), extract("month", Recommendation.generated_at)
    distinct = (
        select(
            year.label("y"), month.label("m"), Recommendation.sap_material_number.label("material"), Recommendation.sap_plant_code.label("plant")
        )
        .where(Recommendation.generated_at >= period_start, Recommendation.generated_at < exclusive_end)
        .distinct()
        .subquery()
    )
    rows = session.execute(select(distinct.c.y, distinct.c.m, func.count()).group_by(distinct.c.y, distinct.c.m)).all()
    return {(int(y), int(m)): int(n) for y, m, n in rows}


def _first_month(value) -> date | None:
    return date(value.year, value.month, 1) if value is not None else None


def build_performance_trends(session: Session, period_end: date, today: date) -> PerformanceTrends:
    months = trend_months(period_end)
    start, exclusive_end = months[0], exclusive_end_of(period_end)

    records = generated_by_month(session, start, exclusive_end)
    plants = _material_plants_by_month(session, start, exclusive_end)
    events = {
        key: _events_by_month(session, action, start, exclusive_end)
        for action, key, _label, _note in LEDGER_ROWS
    }
    # Where each source's history begins, over all time: a month before it has no figure, not a 0.
    recs_from = _first_month(session.execute(select(func.min(Recommendation.generated_at))).scalar())
    approvals_from = _first_month(session.execute(select(func.min(ApprovalLedgerEntry.timestamp))).scalar())
    rows, snapshot_note = _load(session, months)

    points: list[PerformanceMonth] = []
    for m in months:
        state = month_state(m, period_end, today)
        started = state != "not_started"
        key = (m.year, m.month)
        has_recs = started and recs_from is not None and m >= recs_from
        has_events = started and approvals_from is not None and m >= approvals_from
        row = rows.get(m)
        month_end = row is not None and row.status == STATUS_FINAL and bool(row.month_end_valid)
        points.append(
            PerformanceMonth(
                month=m,
                label=month_label(m),
                state=state,
                records_generated=records.get(key, 0) if has_recs else None,
                material_plants_generated=plants.get(key, 0) if has_recs else None,
                submitted=events["submitted_for_approval"].get(key, 0) if has_events else None,
                approved=events["approval_decisions"].get(key, 0) if has_events else None,
                rejected=events["rejections"].get(key, 0) if has_events else None,
                sent_back=events["sent_back"].get(key, 0) if has_events else None,
                excess_state="MONTH_END" if month_end else ("MISSING" if row is None else "ROLLING"),
                excess_count=row.excess_count if month_end else None,
                excess_opportunity=Decimal(str(row.excess_opportunity)) if month_end and row.excess_opportunity is not None else None,
                currency=row.currency if month_end else None,
            )
        )

    notes: list[str] = []
    if snapshot_note:
        notes.append(snapshot_note)
    else:
        missing = [p.label for p in points if p.excess_state == "MISSING" and p.state != "not_started"]
        rolling = [p.label for p in points if p.excess_state == "ROLLING"]
        if missing:
            notes.append(f"No saved month-end snapshot for {', '.join(missing)}: those months have no excess figure, and a past month cannot be rebuilt.")
        if rolling:
            notes.append(f"{', '.join(rolling)} {'has' if len(rolling) == 1 else 'have'} only a rolling or late snapshot, which is not a month-end figure and is left out.")
    if sum(1 for p in points if p.excess_state == "MONTH_END") < MIN_TREND_POINTS:
        notes.append("Fewer than two months have a month-end excess snapshot, so there is no excess trend yet.")
    if recs_from is None:
        notes.append("No recommendation has been generated yet.")
    elif recs_from > months[0]:
        notes.append(f"Recommendations were first generated in {month_label(recs_from)}; earlier months have no figure.")
    if approvals_from is None:
        notes.append("No approval event has been recorded yet.")
    elif approvals_from > months[0]:
        notes.append(f"The approval history begins in {month_label(approvals_from)}; earlier months have no figure.")
    notes.append(IMPLEMENTATION_NOTE)
    return PerformanceTrends(months=points, recommendation_history_from=recs_from, approval_history_from=approvals_from, notes=notes)

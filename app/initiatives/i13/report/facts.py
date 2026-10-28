"""Every figure the summary report states, read without a model.

The report is a summary of the I13 screens, so it reads what they read: the
served snapshot (in memory or the SQL store -- both are handled, as every I13
route handles both) and the ACT exception table. Nothing here recomputes a
metric. A count that disagrees with the dashboard would be a bug in this file,
not a second opinion.

Kept cheap on purpose. On the SQL store every figure is a COUNT or a short
TOP-N over indexed columns -- the report must never become the query that
competes with a snapshot build for Azure SQL. The one list that would need a
scan (the non-movers themselves) is reported as a count; the screen has the
rows. Reclassification is left out: VZI took it off the screens, and the report
summarises the screens.

The result is a plain JSON-ready dict. It is stored verbatim beside the
narrative, so a report can be re-read months later exactly as it was written.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.initiatives.i13.act.domain import ExceptionStatus, ExceptionType
from app.initiatives.i13.config import get_i13_config
from app.initiatives.i13.models import AcquiredVsPlanStatus, AgingBand
from app.initiatives.i13.report.labels import plant_label
from app.initiatives.i13.snapshot import I13Snapshot, current_plans
from app.initiatives.i13.snapshot_store import kinds
from app.initiatives.i13.snapshot_store.reader import SqlSnapshot
from app.initiatives.i13.summary import summary_from_snapshot
from app.initiatives.i8.material_number import normalise
from app.models.i13_act_exception import ActExceptionRecord
from app.models.i13_snapshot_store import I13SnapshotRecord
from app.shared.material_names import material_names
from app.shared.material_scope import MaterialScope

#: How many rows each "worst first" list carries. Enough to name the problem,
#: few enough that the narrative can mention them and the page stays a page.
TOP_N = 10
OLDEST_EXCEPTIONS = 5

#: Statuses that still need someone to act.
UNRESOLVED = (ExceptionStatus.OPEN, ExceptionStatus.AWAITING_REQUESTER, ExceptionStatus.ESCALATED)


def _num(value: Decimal | int | float | None) -> int | float | None:
    """JSON-ready, and written the way a person would read it."""
    if value is None:
        return None
    value = Decimal(value)
    if value == value.to_integral_value():
        return int(value)
    return float(round(value, 2))


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def _names(materials: list[str]) -> dict[str, str]:
    return material_names(materials)


def _describe(names: dict[str, str], material: str) -> str | None:
    key = normalise(material)
    return names.get(key) if key else None


# --- snapshot reads -----------------------------------------------------------


def _summary(snapshot: I13Snapshot | SqlSnapshot, db: Session, plant: str | None):
    if isinstance(snapshot, SqlSnapshot):
        return snapshot.summary(snapshot.current_plans(), plant=plant)
    return summary_from_snapshot(snapshot, current_plans(db, snapshot), plant=plant)


def _acquired_vs_plan(snapshot: I13Snapshot | SqlSnapshot, plant: str | None) -> dict[str, int]:
    counts = {status.value: 0 for status in AcquiredVsPlanStatus}
    if isinstance(snapshot, SqlSnapshot):
        # One grouped count over the indexed status column (f2 on WATCH rows,
        # see kinds.watch) -- no payload is read.
        R = I13SnapshotRecord
        stmt = (
            select(R.f2, func.count())
            .where(R.version == snapshot.version, R.kind == kinds.WATCH, R.oar == True)  # noqa: E712
            .group_by(R.f2)
        )
        if plant:
            stmt = stmt.where(R.plant == plant)
        for status, count in snapshot.db.execute(stmt).all():
            counts[status] = counts.get(status, 0) + count
        return counts
    for metric in snapshot.watch.values():
        if metric.material_scope is MaterialScope.OAR and (not plant or metric.plant == plant):
            counts[metric.acquired_vs_plan_status.value] += 1
    return counts


def _grni(snapshot: I13Snapshot | SqlSnapshot, plant: str | None):
    """(open line count, the longest-waiting lines) -- OAR only, as the GRNI tab."""
    if isinstance(snapshot, SqlSnapshot):
        rows, total = snapshot.grni(plant=plant, oar_only=True, limit=TOP_N)
        return total, rows
    items = [
        item
        for item in snapshot.grni_entries
        if item.material_scope is MaterialScope.OAR and (not plant or item.ledger.plant == plant)
    ]
    return len(items), items[:TOP_N]


# --- ACT exception table -----------------------------------------------------


def _exception_counts(db: Session, plant: str | None) -> tuple[dict[str, int], dict[str, int]]:
    """(by status, by type) in one grouped query."""
    stmt = select(ActExceptionRecord.status, ActExceptionRecord.exception_type, func.count()).group_by(
        ActExceptionRecord.status, ActExceptionRecord.exception_type
    )
    if plant:
        stmt = stmt.where(ActExceptionRecord.plant == plant)
    by_status = {s.value: 0 for s in ExceptionStatus}
    by_type = {t.value: 0 for t in ExceptionType}
    for status, exception_type, count in db.execute(stmt).all():
        by_status[status] = by_status.get(status, 0) + count
        by_type[exception_type] = by_type.get(exception_type, 0) + count
    return by_status, by_type


def _oldest_unresolved(db: Session, plant: str | None) -> list[ActExceptionRecord]:
    stmt = (
        select(ActExceptionRecord)
        .where(ActExceptionRecord.status.in_([s.value for s in UNRESOLVED]))
        .order_by(ActExceptionRecord.detected_at, ActExceptionRecord.exception_id)
        .limit(OLDEST_EXCEPTIONS)
    )
    if plant:
        stmt = stmt.where(ActExceptionRecord.plant == plant)
    return list(db.execute(stmt).scalars().all())


def _days_since(moment: datetime | None, today: date) -> int | None:
    if moment is None:
        return None
    return (today - moment.date()).days


# --- the whole set -------------------------------------------------------------


def build_report_facts(
    snapshot: I13Snapshot | SqlSnapshot,
    db: Session,
    *,
    plant: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Every figure the report may state, for one plant or all of them."""
    today = today or datetime.now(timezone.utc).date()
    summary = _summary(snapshot, db, plant)
    total = summary.total_oar_positions

    grni_total, grni_rows = _grni(snapshot, plant)
    by_status, by_type = _exception_counts(db, plant)
    oldest = _oldest_unresolved(db, plant)

    names = _names([g.ledger.material for g in grni_rows] + [e.material for e in oldest])

    return {
        "scope": {"plant": plant, "label": plant_label(plant)},
        "data_as_of": snapshot.reference_date.isoformat(),
        "snapshot_version": snapshot.version,
        "snapshot_built_at": snapshot.built_at.isoformat() if snapshot.built_at else None,
        "valuation_is_mocked": summary.valuation_is_mocked,
        "positions": {
            "total": total,
            AgingBand.FAST.value: summary.fast_moving_count,
            AgingBand.SLOW.value: summary.slow_moving_count,
            AgingBand.NON_MOVING.value: summary.non_moving_count,
            "fast_pct": _pct(summary.fast_moving_count, total),
            "slow_pct": _pct(summary.slow_moving_count, total),
            "non_moving_pct": _pct(summary.non_moving_count, total),
        },
        "plans": {
            "plan_breach_count": summary.plan_breach_count,
            "no_plan_count": summary.no_plan_count,
            "reference_plan_count": summary.reference_plan_count,
            "acquired_vs_plan": _acquired_vs_plan(snapshot, plant),
        },
        "gr_not_issued": {
            "threshold_days": get_i13_config().watch.gr_not_issued_threshold_days,
            "position_count": summary.gr_not_issued_30_day_count,
            "open_line_count": grni_total,
            "longest_waiting": [
                {
                    "material": g.ledger.material,
                    "description": _describe(names, g.ledger.material),
                    "plant": plant_label(g.ledger.plant),
                    "po_line": f"{g.ledger.po_number}/{g.ledger.po_item}",
                    "outstanding_quantity": _num(g.outstanding_quantity),
                    "days_since_gr": g.days_since_gr,
                }
                for g in grni_rows
            ],
        },
        "exceptions": {
            "total": sum(by_status.values()),
            "unresolved": sum(by_status.get(s.value, 0) for s in UNRESOLVED),
            "by_status": by_status,
            "by_type": by_type,
            "oldest_unresolved": [
                {
                    "exception_id": e.exception_id,
                    "type": e.exception_type,
                    "status": e.status,
                    "material": e.material,
                    "description": _describe(names, e.material),
                    "plant": plant_label(e.plant),
                    "reservation": (
                        f"{e.reservation_number}/{e.reservation_item}" if e.reservation_number else None
                    ),
                    "days_open": _days_since(e.detected_at, today),
                }
                for e in oldest
            ],
        },
    }

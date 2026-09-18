"""W6.6: maps persisted W6.3 ``WatchMetricMart`` rows onto the small,
ORM-free ``WatchGrniSnapshot`` the ACT domain layer actually needs (just the
already-computed GRNI evidence) -- see ``app.initiatives.i13.act.domain
.WatchGrniSnapshot``'s docstring for why this indirection exists: it is what
keeps SQLAlchemy out of ``app.initiatives.i13.act``.

A pure field-for-field read, never a recomputation -- see
``app.initiatives.i13.watch_mart.get_watch_metric``/``list_watch_metrics``
for the mart reads this wraps.
"""

from __future__ import annotations

from app.initiatives.i13.act.domain import WatchGrniSnapshot
from app.models.i13_watch_mart import WatchMetricMart


def to_grni_snapshot(row: WatchMetricMart) -> WatchGrniSnapshot:
    return WatchGrniSnapshot(
        material=row.material,
        plant=row.plant,
        gr_not_issued_flag=row.gr_not_issued_flag,
        gr_not_issued_days_since_gr=row.gr_not_issued_days_since_gr,
        gr_not_issued_threshold_days=row.gr_not_issued_threshold_days,
    )


def build_grni_snapshot_index(rows: list[WatchMetricMart]) -> dict[tuple[str, str], WatchGrniSnapshot]:
    return {(row.material, row.plant): to_grni_snapshot(row) for row in rows}

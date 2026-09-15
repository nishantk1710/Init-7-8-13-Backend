"""One assembled view of I08, built once and reused.

This is where W5.1 and W5.2 meet. The register is built first, its open lines
are indexed by material and plant, and that index gives every universe row its
``hasOpenRepair`` flag. Building it in that order is what keeps the repair-PO
rule in exactly one module: the universe never needs to know what an item
category is.

Why it is cached
----------------
Assembling the whole thing touches roughly 400,000 rows and takes about six
seconds: the EKPO candidate pull alone is 82,718 rows, because the Pstyp
predicate is deliberately applied in Python rather than in the query (ruling
5.2) and cannot narrow the pull. Paying that on every request would make the
register unusable in the UI, and W5.4 is rendering against it.

The cache is safe here for one specific reason: **the source is a static July
snapshot.** Nothing writes to it -- every I08 endpoint is read-only, and there
is no write path to SAP or to our database anywhere in W5.1 or W5.2.

That assumption expires at CPI cutover. When the source becomes live, this
module is the one place that has to change: a TTL, or an explicit invalidation
hook on the ingestion run. The rest of I08 asks for a snapshot and does not care
how old it is, so that change stays here.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i8.attestation import AttestationCoverage, coverage as attestation_coverage
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.declarations import DeclarationRow, build_queue
from app.initiatives.i8.exceptions import ExceptionItem, ExceptionStats, build_exceptions
from app.initiatives.i8.register import (
    RegisterStats,
    RepairLine,
    load_repair_lines,
    open_repair_index,
)
from app.initiatives.i8.universe import UniverseRow, UniverseStats, load_universe
from app.initiatives.i8.vendors import VendorTurnaround, vendor_turnaround

logger = get_logger(__name__)


@dataclass(frozen=True)
class Snapshot:
    """Everything I08 serves, as of one reference date."""

    reference_date: date
    built_at: datetime
    build_seconds: float

    lines: tuple[RepairLine, ...]
    register_stats: RegisterStats

    universe: tuple[UniverseRow, ...]
    universe_stats: UniverseStats

    vendors: tuple[VendorTurnaround, ...]

    def line(self, document: str, item: str) -> RepairLine | None:
        """One repair line by its (EBELN, EBELP) key."""
        return self._lines_by_key.get((document, item))

    def material(self, material_id: str) -> tuple[UniverseRow, ...]:
        """Every plant row for one material. Empty if it is not repairable."""
        return tuple(row for row in self.universe if row.material_id == material_id)

    def lines_for_material(self, material_id: str) -> tuple[RepairLine, ...]:
        return tuple(line for line in self.lines if line.material_id == material_id)

    @property
    def _lines_by_key(self) -> dict[tuple[str, str], RepairLine]:
        # Built lazily and memoised on the instance. The dataclass is frozen,
        # so object.__setattr__ is how a cached attribute gets there.
        cached = self.__dict__.get("_key_index")
        if cached is None:
            cached = {line.key: line for line in self.lines}
            object.__setattr__(self, "_key_index", cached)
        return cached


def build_snapshot(
    db: Session, cfg: I8Settings | None = None, *, today: date | None = None
) -> Snapshot:
    """Assemble the register, the universe and the vendor analytics.

    Always does the work. :func:`get_snapshot` is the cached entry point.
    """
    cfg = cfg or get_i8_settings()
    reference_date = today or cfg.reference_date_value or date.today()
    started = time.monotonic()

    lines, register_stats = load_repair_lines(db, cfg, today=reference_date)
    universe, universe_stats = load_universe(
        db, cfg, open_repair_index=open_repair_index(lines)
    )
    vendors = vendor_turnaround(lines)

    elapsed = time.monotonic() - started
    logger.info(
        "I08 snapshot built in %.2fs: %d repair lines, %d universe rows, %d vendors",
        elapsed,
        len(lines),
        len(universe),
        len(vendors),
    )
    return Snapshot(
        reference_date=reference_date,
        built_at=datetime.now(timezone.utc),
        build_seconds=round(elapsed, 2),
        lines=tuple(lines),
        register_stats=register_stats,
        universe=tuple(universe),
        universe_stats=universe_stats,
        vendors=tuple(vendors),
    )


# The cache. A lock rather than a bare global because FastAPI runs synchronous
# endpoints in a thread pool, so two requests really can arrive at once -- and
# two concurrent builds would spend twelve seconds producing one answer.
_lock = threading.Lock()
_snapshot: Snapshot | None = None


def get_snapshot(
    db: Session, cfg: I8Settings | None = None, *, refresh: bool = False
) -> Snapshot:
    """The cached snapshot, built on first use."""
    global _snapshot
    with _lock:
        if _snapshot is None or refresh:
            _snapshot = build_snapshot(db, cfg)
        return _snapshot


def reset_snapshot() -> None:
    """Drop the cached snapshot.

    For tests, and for any future code that reloads the underlying data.
    """
    global _snapshot
    with _lock:
        _snapshot = None
    # The attestation view is derived from the snapshot's lines, so it cannot
    # outlive it.
    reset_attestation_view()


# --- The attestation view (W5.3) ------------------------------------------
#
# Cached separately from the snapshot, and that split is the whole design.
#
# The snapshot is built from the July extract, which never changes, so it is
# cached until the process restarts. Attestations are the one thing in I08 that
# DOES change while the process runs -- somebody submits one -- so a view
# derived from them cannot share that lifetime. Fold it into the snapshot and a
# planner records an attestation, reloads the queue, and sees their own entry
# missing. That is the failure that looks like a bug in a demo.
#
# So: same batch shape, separate lifetime. One pass over the register, one query
# for the whole attestation table, invalidated explicitly when a write happens.
# What the task plan rules out is a query PER LINE, and there is not one here.


@dataclass(frozen=True)
class AttestationView:
    """Coverage, the declaration queue and the exception queue, built together.

    One object because they are three readings of a single pass: which repair
    lines have an attestation. Building them separately would mean matching
    1,225 lines against the attestation table three times to get three answers
    that must agree.
    """

    coverage: AttestationCoverage
    declarations: tuple[DeclarationRow, ...]
    exceptions: tuple[ExceptionItem, ...]
    exception_stats: ExceptionStats
    built_at: datetime


def build_attestation_view(
    db: Session, snapshot: Snapshot, cfg: I8Settings | None = None
) -> AttestationView:
    """Match every repair line against the attestation table. Always does the work."""
    cfg = cfg or get_i8_settings()
    cover = attestation_coverage(db, snapshot.lines, cfg)
    exceptions, exception_stats = build_exceptions(snapshot.lines, cover)
    return AttestationView(
        coverage=cover,
        declarations=tuple(build_queue(snapshot.lines, cover)),
        exceptions=tuple(exceptions),
        exception_stats=exception_stats,
        built_at=datetime.now(timezone.utc),
    )


_attestation_lock = threading.Lock()
_attestation_view: AttestationView | None = None


def get_attestation_view(
    db: Session,
    snapshot: Snapshot,
    cfg: I8Settings | None = None,
    *,
    refresh: bool = False,
) -> AttestationView:
    """The cached attestation view, rebuilt on first use and after any write."""
    global _attestation_view
    with _attestation_lock:
        if _attestation_view is None or refresh:
            _attestation_view = build_attestation_view(db, snapshot, cfg)
        return _attestation_view


def reset_attestation_view() -> None:
    """Drop the cached view. **Call this after recording an attestation.**

    Explicit rather than automatic, and called at the write site in the router,
    so that the one place that changes the data is also the place that says so.
    A TTL would be the alternative and would mean a planner's own submission
    takes up to N seconds to appear, which is indistinguishable from broken.
    """
    global _attestation_view
    with _attestation_lock:
        _attestation_view = None

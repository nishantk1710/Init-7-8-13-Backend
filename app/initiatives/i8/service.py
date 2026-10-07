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

Nothing writes to the source through this application -- every I08 endpoint is
read-only, and there is no write path to SAP or to our database anywhere in
W5.1 or W5.2. For a long while that was the whole argument, because the source
was a static July extract: cache it until the process restarts and it can never
be stale.

**That assumption has expired, and this is the change it asked for.** The raw
layer is now reloaded underneath a running process -- a CSV full pull replaces
``raw_<table>`` whole (``app/ingest/csv_load.py``) -- so "nothing writes to it"
is true of this application and false of the database. A pull that landed at
05:00 stayed invisible until somebody restarted the App Service, and the UI
went on reporting yesterday's figures with no sign that it was doing so.

So the snapshot now records what it was built from (:func:`source_state`) and
:func:`check_source_fingerprint` rebuilds it when that moves. The reference
date is part of the fingerprint for the same reason it is part of I13's: with
``I8_REFERENCE_DATE`` unset the snapshot measures aging as of the day it was
built, so one that survives midnight reports yesterday's overdue counts under
today's heading. The rest of I08 still asks for a snapshot and still does not
care how old it is, so the change stays here.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_sessionmaker, statement_timeout
from app.core.logging import get_logger
from app.initiatives.i8.acquisitions import (
    NEW_ACQUISITION_KIND,
    NewAcquisition,
    find_new_acquisitions,
    load_justifications,
)
from app.initiatives.i8.attestation import AttestationCoverage, coverage as attestation_coverage
from app.initiatives.i8.coding_candidates import CodingCandidate, ScreenStats, screen
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.declarations import DeclarationRow, build_queue
from app.initiatives.i8.exceptions import ExceptionItem, ExceptionStats, build_exceptions
from app.initiatives.i8.register import (
    RegisterStats,
    RepairLine,
    fetch_candidate_lines,
    load_repair_lines,
    open_repair_index,
    timed_step,
)
from app.initiatives.i8.universe import UniverseRow, UniverseStats, load_universe
from app.initiatives.i8.vendors import VendorTurnaround, vendor_turnaround
from app.models import IngestionRun
from app.shared.snapshot_builds import exclusive_build

logger = get_logger(__name__)


# The raw tables every I08 view reads -- twelve, for the eleven views listed in
# ``app/initiatives/i8/views.py``, because v_zmm065 is a union of two.
#
# Written out rather than derived from that list: a view name does not say what
# fills it, and guessing ``v_x -> raw_x`` would silently miss the zmm065 pair
# and silently invent tables for any future view over something else. A table
# missing here is a reload I08 will not notice.
SOURCE_TABLES: tuple[str, ...] = (
    "raw_mara",
    "raw_makt",
    "raw_marc",
    "raw_mard",
    "raw_ekko",
    "raw_ekpo",
    "raw_eket",
    "raw_ekbe",
    "raw_mseg",
    "raw_lfa1",
    "raw_zmm065_bmm",
    "raw_zmm065_gb",
)


@dataclass(frozen=True)
class SourceState:
    """What the raw layer looked like at one moment."""

    fingerprint: str
    """Opaque; only ever compared for equality."""

    loaded_at: datetime | None
    """The newest successful load across :data:`SOURCE_TABLES`, or None when
    none has ever run. Served to the UI so a screen can say how fresh its data
    is instead of naming a source it has no way to see."""


def source_state(db: Session, *, reference_date: date) -> SourceState:
    """What the snapshot would be built from right now.

    Cheap, because it runs on a timer: one grouped query over ``ingestion_run``.
    The run id is in the fingerprint as well as the timestamp, so a reload that
    finishes within a clock tick of the one before it still registers.
    """
    rows = db.execute(
        select(
            IngestionRun.target_table,
            func.max(IngestionRun.id),
            func.max(IngestionRun.finished_at),
        )
        .where(
            IngestionRun.status == "succeeded",
            IngestionRun.target_table.in_(SOURCE_TABLES),
        )
        .group_by(IngestionRun.target_table)
        .order_by(IngestionRun.target_table)
    ).all()
    parts = [f"{table}:{run_id}:{finished}" for table, run_id, finished in rows]
    parts.append(f"date:{reference_date.isoformat()}")
    loaded = [finished for _, _, finished in rows if finished is not None]
    return SourceState(
        fingerprint="|".join(parts),
        loaded_at=max(loaded) if loaded else None,
    )


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

    acquisitions: tuple[NewAcquisition, ...] = ()
    """New 80-series purchase lines, for the UNJUSTIFIED_ACQUISITION check.
    From the same EKPO pull as the register, so it shares its lifetime."""

    source_fingerprint: str = ""
    """What :data:`SOURCE_TABLES` looked like when this build started. A
    different one on a later check means the raw layer moved underneath."""

    source_loaded_at: datetime | None = None
    """When the newest of those tables last loaded successfully."""

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

    # BEFORE the build, not after, and the difference matters. A reload that
    # lands while the build is running leaves this snapshot holding a mixture
    # of both; recording the fingerprint taken first means the next check sees
    # a difference and rebuilds. Recorded afterwards it would claim to be the
    # new data, and the mixture would never be corrected.
    source = source_state(db, reference_date=reference_date)

    with statement_timeout(db, get_settings().i8_snapshot_statement_timeout_seconds):
        # One EKPO pull for both readings of it: repair lines and new purchases.
        with timed_step("PO lines (EKPO)"):
            candidates = fetch_candidate_lines(db)
        lines, register_stats = load_repair_lines(
            db, cfg, today=reference_date, candidates=candidates
        )
        acquisitions = find_new_acquisitions(candidates, cfg)
        with timed_step("repairable universe"):
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
        acquisitions=tuple(acquisitions),
        source_fingerprint=source.fingerprint,
        source_loaded_at=source.loaded_at,
    )


class SnapshotBuilding(Exception):
    """No snapshot yet: a build is still running."""

    def __init__(self, started_at: datetime | None) -> None:
        super().__init__("The I08 snapshot is still being built")
        self.started_at = started_at


class SnapshotFailed(Exception):
    """No snapshot: the last build failed."""


@dataclass
class _State:
    snapshot: Snapshot | None = None
    building_since: datetime | None = None
    last_error: str | None = None
    failed_at: float = 0.0
    thread: threading.Thread | None = None
    last_check: float = 0.0
    """``time.monotonic()`` of the last fingerprint check -- see
    :func:`check_source_fingerprint`, which is rate-limited by it."""


_state = _State()
# Guards `_state`; held only briefly, never across a build.
_state_lock = threading.Lock()
# Serialises builds, and is what a caller waits on for the build in progress.
_build_lock = threading.Lock()
_RETRY_AFTER_FAILURE_SECONDS = 60.0


def _describe(exc: Exception) -> str:
    # First line only: SQLAlchemy appends the failing SQL, which is not for a response body.
    first_line = (str(exc).splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first_line}"


def _build_locked(db: Session, cfg: I8Settings | None, reason: str) -> Snapshot:
    """Build and record the outcome. The caller holds ``_build_lock``."""
    with _state_lock:
        _state.building_since = datetime.now(timezone.utc)
    logger.info("I08 snapshot build started (%s)", reason)
    try:
        with exclusive_build("I08"):
            snapshot = build_snapshot(db, cfg)
    except Exception as exc:
        logger.exception("I08 snapshot build failed (%s)", reason)
        message = _describe(exc)
        with _state_lock:
            _state.last_error = message
            _state.failed_at = time.monotonic()
            _state.building_since = None
        raise SnapshotFailed(message) from exc
    with _state_lock:
        _state.snapshot = snapshot
        _state.last_error = None
        _state.building_since = None
    return snapshot


def get_snapshot(
    db: Session,
    cfg: I8Settings | None = None,
    *,
    refresh: bool = False,
    wait_seconds: float | None = None,
) -> Snapshot:
    """The cached snapshot, built on first use.

    A build already running is waited for -- at most ``wait_seconds`` when
    given, then :class:`SnapshotBuilding`. A build that failed within the last
    minute raises :class:`SnapshotFailed` instead of being re-run by every caller.

    A bounded caller (a route) never builds in its own thread: it starts the
    background build, or joins the one running. Built inline, the first request
    after a failure was held for the whole build -- minutes on Azure SQL, and
    longer now that the build waits its turn behind an I13 one.
    """
    with _state_lock:
        if _state.snapshot is not None and not refresh:
            return _state.snapshot
    if wait_seconds is not None and not refresh:
        return _await_background_build(wait_seconds)
    if not _build_lock.acquire(timeout=-1 if wait_seconds is None else wait_seconds):
        with _state_lock:
            raise SnapshotBuilding(_state.building_since)
    try:
        with _state_lock:
            snapshot, last_error = _state.snapshot, _state.last_error
            failed_ago = time.monotonic() - _state.failed_at
        if not refresh:
            if snapshot is not None:
                return snapshot
            if last_error is not None and failed_ago < _RETRY_AFTER_FAILURE_SECONDS:
                raise SnapshotFailed(last_error)
        return _build_locked(db, cfg, "refresh" if refresh else "first request")
    finally:
        _build_lock.release()


def _await_background_build(wait_seconds: float) -> Snapshot:
    with _state_lock:
        last_error = _state.last_error
        failed_ago = time.monotonic() - _state.failed_at
    if last_error is not None and failed_ago < _RETRY_AFTER_FAILURE_SECONDS:
        raise SnapshotFailed(last_error)
    start_background_build("retry after failure" if last_error is not None else "first request")
    thread = build_thread()
    if thread is not None:
        thread.join(wait_seconds)
    with _state_lock:
        if _state.snapshot is not None:
            return _state.snapshot
        if thread is not None and not thread.is_alive() and _state.last_error is not None:
            raise SnapshotFailed(_state.last_error)
        raise SnapshotBuilding(_state.building_since)


def build_thread() -> threading.Thread | None:
    """The latest background build's thread, running or finished; None if none was started."""
    with _state_lock:
        return _state.thread


def start_background_build(reason: str, *, force: bool = False) -> bool:
    """Build on a daemon thread with its own session. False if one is already running.

    ``force`` rebuilds even when a snapshot is already cached, which is what
    the fingerprint watcher needs. The cached one keeps answering throughout --
    :func:`_build_locked` swaps it in only once the new one is assembled -- so
    a rebuild costs readers nothing but the staleness they already had.
    """
    with _state_lock:
        if _state.thread is not None and _state.thread.is_alive():
            return False
        if _state.snapshot is None:
            # Set here as well as in _build_locked, so a route asking before the
            # thread has reached the build still gets a startedAt.
            _state.building_since = datetime.now(timezone.utc)
        thread = threading.Thread(
            target=_build_in_background,
            args=(reason, force),
            name="i8-snapshot",
            daemon=True,
        )
        _state.thread = thread
    thread.start()
    return True


def _build_in_background(reason: str, force: bool = False) -> None:
    with _build_lock:
        with _state_lock:
            if _state.snapshot is not None and not force:
                return
        db = get_sessionmaker()()
        try:
            _build_locked(db, None, reason)
        except SnapshotFailed:
            pass  # already logged and recorded for the next request to report
        finally:
            db.close()


def reset_snapshot() -> None:
    """Drop the cached snapshot.

    For tests, and for any future code that reloads the underlying data.
    """
    with _state_lock:
        _state.snapshot = None
        _state.last_error = None
        _state.failed_at = 0.0
        _state.building_since = None
        _state.last_check = 0.0
    # The attestation view and the coding screen are both derived from the
    # snapshot, so neither can outlive it.
    reset_attestation_view()
    reset_coding_screen()


def check_source_fingerprint(db: Session, *, min_interval_seconds: float | None = None) -> bool:
    """Rebuild in the background when the raw layer, or the date, has moved.

    Rate-limited to one check per ``I8_SNAPSHOT_CHECK_INTERVAL_SECONDS``; pass
    ``min_interval_seconds=0`` from a caller that is itself the timer. Returns
    True when a rebuild was started.

    Returns False when there is no snapshot yet: a first build is already the
    responsibility of start-up or the first request, and racing another one
    against it would only contend for the same lock.
    """
    interval = (
        get_settings().i8_snapshot_check_interval_seconds
        if min_interval_seconds is None
        else min_interval_seconds
    )
    with _state_lock:
        snapshot = _state.snapshot
        if snapshot is None or time.monotonic() - _state.last_check < interval:
            return False
        _state.last_check = time.monotonic()

    cfg = get_i8_settings()
    current = source_state(db, reference_date=cfg.reference_date_value or date.today())
    if current.fingerprint == snapshot.source_fingerprint:
        return False
    logger.info(
        "I08 source data changed (loaded %s, snapshot built from %s); rebuilding",
        current.loaded_at,
        snapshot.source_loaded_at,
    )
    return start_background_build("source data changed", force=True)


# --- The attestation view (W5.3) ------------------------------------------
#
# Cached separately from the snapshot, and that split is the whole design.
#
# The snapshot turns over when its source does, which is a reload at most --
# minutes at the very fastest, usually a day. Attestations change far more
# often than that -- somebody submits one and must see it at once -- so a view
# derived from them cannot share that lifetime. Fold it into the snapshot and a
# planner records an attestation, reloads the queue, and sees their own entry
# missing. That is the failure that looks like a bug in a demo.
#
# So: same batch shape, separate lifetime. One pass over the register, one query
# for the whole attestation table, invalidated explicitly when a write happens.
# What the task plan rules out is a query PER LINE, and there is not one here.
#
# Explicit invalidation only covers writes made through THIS process's router.
# Two other writers exist: `demo_seed` (a separate process -- after `--clear`
# a running server kept serving the demo coverage until it restarted) and the
# WS7 justification endpoints, which the UNJUSTIFIED_ACQUISITION check reads.
# So every read also compares a cheap fingerprint of both tables -- a count and
# a latest timestamp each, two aggregate queries -- and rebuilds when it moved.
# The tables are append-only apart from the demo clear, which changes the count.


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
    source_fingerprint: tuple | None = None
    """What the attestation and justification tables looked like when this was
    built. A different fingerprint on read means another writer moved them."""

    @property
    def declaration_status_by_line(self) -> dict[tuple[str, str], str]:
        """(document, item) -> declaration status, for the register.

        The register carries a declarationStatus column that the UI renders.
        Until W5.3 it was a hard-coded "Required" placeholder with a note saying
        W5.3 owned it; this is that ownership arriving. Memoised on the instance
        because the register maps 1,225 rows and rebuilding the index per row
        would be the per-row cost this whole view exists to avoid.
        """
        cached = self.__dict__.get("_status_index")
        if cached is None:
            cached = {
                # DeclarationRow.related_repair_id is "{document}-{item}", the
                # same key the register uses, but the line's own tuple is what
                # the caller holds -- so it is rebuilt from the id's parts.
                tuple(row.related_repair_id.rsplit("-", 1)): row.status
                for row in self.declarations
            }
            object.__setattr__(self, "_status_index", cached)
        return cached


def source_fingerprint(db: Session) -> tuple:
    """A cheap summary of every table the attestation view is derived from.

    Count and latest timestamp of the attestation table, and of the
    NEW_ACQUISITION justifications. Two aggregate queries, portable across
    Postgres and Azure SQL.
    """
    from app.assistant.models import Justification
    from app.initiatives.i8.models import RepairAttestation

    attestations = db.execute(
        select(func.count(), func.max(RepairAttestation.attested_at))
    ).one()
    justifications = db.execute(
        select(func.count(), func.max(Justification.recorded_at)).where(
            Justification.kind == NEW_ACQUISITION_KIND
        )
    ).one()
    return (tuple(attestations), tuple(justifications))


def build_attestation_view(
    db: Session,
    snapshot: Snapshot,
    cfg: I8Settings | None = None,
    *,
    fingerprint: tuple | None = None,
) -> AttestationView:
    """Match every repair line against the attestation table, and every new
    purchase against the justifications. Always does the work."""
    cfg = cfg or get_i8_settings()
    cover = attestation_coverage(db, snapshot.lines, cfg)
    exceptions, exception_stats = build_exceptions(
        snapshot.lines,
        cover,
        cutover=cfg.attestation_cutover_date_value,
        acquisitions=snapshot.acquisitions,
        justifications=load_justifications(db),
        justification_window_days=cfg.justification_window_days,
        justification_cutover=cfg.justification_cutover_date_value,
    )
    return AttestationView(
        coverage=cover,
        declarations=tuple(build_queue(snapshot.lines, cover)),
        exceptions=tuple(exceptions),
        exception_stats=exception_stats,
        built_at=datetime.now(timezone.utc),
        source_fingerprint=fingerprint,
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
    """The cached attestation view, rebuilt on first use, after any write
    through this process, and whenever the source tables moved underneath it."""
    global _attestation_view
    fingerprint = source_fingerprint(db)
    with _attestation_lock:
        if (
            _attestation_view is None
            or refresh
            or _attestation_view.source_fingerprint != fingerprint
        ):
            _attestation_view = build_attestation_view(
                db, snapshot, cfg, fingerprint=fingerprint
            )
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


# --- The coding-candidate screen (W5.5) -----------------------------------
#
# Cached separately again, and for a third distinct lifetime.
#
#   snapshot           the frozen July extract -- never changes
#   attestation view   our own table -- changes on every write
#   coding screen      the extract AND a model -- changes only when re-run
#
# The screen is slow in a way the others are not: 41 materials at roughly six
# seconds each is four minutes, because every call is a separate round trip to
# gpt-4o. Nothing about that is fixable here, so it is cached hard and the model
# pass is opt-in rather than implicit. Re-running it is an explicit request.


@dataclass(frozen=True)
class CodingScreen:
    """One run of the coding-candidate screen."""

    candidates: tuple[CodingCandidate, ...]
    stats: ScreenStats
    built_at: datetime
    build_seconds: float
    used_model: bool
    """False when only the deterministic half ran. The stats say so too, via
    every verdict being UNSCREENED."""


_screen_lock = threading.Lock()
#: Keyed by (use_model, limit) so a fast run and a full run do not evict each
#: other -- a demo typically wants both.
_screens: dict[tuple[bool, int | None], CodingScreen] = {}


def build_coding_screen(
    db: Session,
    snapshot: Snapshot,
    cfg: I8Settings | None = None,
    *,
    use_model: bool = False,
    limit: int | None = None,
) -> CodingScreen:
    """Run the screen. Always does the work."""
    cfg = cfg or get_i8_settings()
    started = time.monotonic()
    candidates, stats = screen(
        db,
        cfg,
        limit=limit,
        # The cross-check the task plan asks for, from the snapshot we already
        # hold rather than a five-second rebuild.
        universe_materials={row.material_id for row in snapshot.universe},
        use_model=use_model,
    )
    return CodingScreen(
        candidates=tuple(candidates),
        stats=stats,
        built_at=datetime.now(timezone.utc),
        build_seconds=round(time.monotonic() - started, 2),
        used_model=use_model,
    )


def get_coding_screen(
    db: Session,
    snapshot: Snapshot,
    cfg: I8Settings | None = None,
    *,
    use_model: bool = False,
    limit: int | None = None,
    refresh: bool = False,
) -> CodingScreen:
    """The cached screen for this (use_model, limit), built on first use."""
    key = (use_model, limit)
    with _screen_lock:
        if refresh or key not in _screens:
            _screens[key] = build_coding_screen(
                db, snapshot, cfg, use_model=use_model, limit=limit
            )
        return _screens[key]


def reset_coding_screen() -> None:
    """Drop every cached screen. For tests, and for a deliberate re-run."""
    with _screen_lock:
        _screens.clear()

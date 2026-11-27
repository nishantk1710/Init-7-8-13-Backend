"""One assembled view of I08, built once and reused.

This is where W5.1 and W5.2 meet. The register is built first -- the repair
lines SAP still has open -- its lines are indexed by material and plant, and
that index gives every universe row its ``hasOpenRepair`` flag. Building it in that order is what keeps the repair-PO
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
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_sessionmaker, statement_timeout
from app.core.logging import get_logger
from app.initiatives.i8.acquisitions import NewAcquisition, load_new_acquisitions
from app.initiatives.i8.attestation import AttestationCoverage, coverage as attestation_coverage
from app.initiatives.i8.coding_candidates import (
    CodingCandidate,
    ScreenStats,
    Verdict,
    reapply_stored_verdicts,
    screen,
)
from app.initiatives.i8.coding_store import verdict_stamp as read_verdict_stamp
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.declarations import DeclarationRow, build_queue
from app.initiatives.i8.new_item_price import MissingPrice
from app.initiatives.i8.register import (
    RegisterStats,
    RepairLine,
    fetch_candidate_lines,
    load_repair_lines,
    with_requisition_details,
    open_repair_index,
    timed_step,
)
from app.initiatives.i8.repair_references import load_repair_references
from app.initiatives.i8.requisitions import RepairRequest, fetch_repair_order_lines, load_repair_requests
from app.initiatives.i8.session_jobs import SessionJob, load_session_jobs
from app.initiatives.i8.staged import staged_sources
from app.initiatives.i8.universe import UniverseRow, UniverseStats, load_universe
from app.models import IngestionRun
from app.shared.snapshot_builds import exclusive_build

logger = get_logger(__name__)


# The tables every I08 view reads -- one per view listed in
# ``app/initiatives/i8/views.py``, except that v_zmm065 reads
# criticality_current rather than a raw table, and that v_ekbe's raw_ekbe is
# left out: I08 has not read goods receipts since 19-Oct-2026 (the register
# follows SAP's "Delivery completed" flag), so an EKBE reload changes nothing
# here. I13 still reads it, through its own snapshot.
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
    "raw_eban",
    # The repair order's reservation lines (v_resb) and stock per condition
    # (v_mchb), read for open repair PRs and usable vs damaged stock from
    # 27-Oct-2026. A reload of either changes what the snapshot holds.
    "raw_resb",
    "raw_mseg",
    "raw_lfa1",
    # Read by the coding screen's New Item Price (v_mbew), and by the snapshot
    # build for stock per condition when I8_STOCK_CONDITION_SOURCE=MBEW
    # (27-Oct-2026; MCHB by default). Listed so a weekly MBEW reload still
    # moves the fingerprint: the rebuild resets the cached coding screen, which
    # would otherwise keep serving last week's prices until a restart.
    "raw_mbew",
    "raw_mchb",
    # What the monthly criticality uploads add up to; an upload writes an
    # ingestion_run row for it (app/integrations/criticality/reports.py).
    "criticality_current",
)


@dataclass(frozen=True)
class TableLoad:
    """When one source table last loaded successfully."""

    table: str
    loaded_at: datetime | None
    """None for a table that has never loaded at all -- which is not the same
    as one that loaded long ago. ``criticality_current`` is None wherever no
    ZMM065 report has been uploaded yet."""


@dataclass(frozen=True)
class SourceState:
    """What the raw layer looked like at one moment."""

    fingerprint: str
    """Opaque; only ever compared for equality."""

    loaded_at: datetime | None
    """The NEWEST successful load across :data:`SOURCE_TABLES`, or None when
    none has ever run.

    Kept as the newest because that is what the fingerprint log reports: the
    raw layer moved at this time. It is the wrong number to put in front of a
    user on its own -- see :attr:`oldest_loaded_at`."""

    oldest_loaded_at: datetime | None = None
    """The OLDEST successful load across the tables that have ever loaded.

    This is what a screen may say, because it is the only claim that cannot
    overstate how fresh the data is: everything behind the screen is at least
    this recent. :attr:`loaded_at` says the opposite -- one table loading this
    morning makes it read "this morning" even where another stopped loading
    weeks ago, which is a failure I08 has actually had (raw_resb, 3-Nov-2026).

    Tables that have never loaded are left out rather than making this None:
    on a deployment with no criticality report uploaded, counting it would
    erase the answer for every other table."""

    per_table: tuple[TableLoad, ...] = ()
    """Every source table and when it last loaded, oldest first, so a screen
    can name the one that is behind instead of only saying that one is."""


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
    seen = {table: finished for table, _, finished in rows}
    # Every table in SOURCE_TABLES, not only the ones with a run: a table that
    # has never loaded is the most interesting row in the list.
    #
    # Never-loaded sorts last rather than first. It is a different statement
    # from "loaded long ago", and ordering None as the oldest would bury the
    # real laggard under tables nobody has ever filled.
    per_table = tuple(
        sorted(
            (TableLoad(table=table, loaded_at=seen.get(table)) for table in SOURCE_TABLES),
            key=lambda t: (
                t.loaded_at is None,
                t.loaded_at or datetime.min.replace(tzinfo=timezone.utc),
            ),
        )
    )
    return SourceState(
        fingerprint="|".join(parts),
        loaded_at=max(loaded) if loaded else None,
        oldest_loaded_at=min(loaded) if loaded else None,
        per_table=per_table,
    )


# The dates that say something HAPPENED, by the view that carries them. Only
# postings and creations belong here.
#
# Not every date in the data is an activity date, and the difference is not
# cosmetic: measured on the production copy (27-Nov-2026), EKET.EINDT reaches
# 2030-09-30 and RESB.BDTER 2028-04-30, because both are dates something is
# WANTED by. A watermark over "the newest date in the data" would tell the
# user the register covers activity into 2030. These three agree with each
# other -- all 2026-11-05 on that same copy -- which is what makes the number
# worth showing at all.
ACTIVITY_DATES: tuple[tuple[str, str], ...] = (
    ("v_ekko", "aedat"),  # purchase order created
    ("v_eban", "badat"),  # requisition raised
    ("v_mseg", "budat"),  # goods movement posted
)


def activity_coverage(db: Session, *, reference_date: date) -> date | None:
    """The newest activity the data holds: nothing after this has reached us.

    The counterpart to :attr:`SourceState.oldest_loaded_at`, and it takes the
    newest where that takes the oldest, for the same reason -- each is the
    bound that cannot overstate freshness. A load time says when we last
    pulled; this says how far what we pulled actually reaches, and the two
    come apart whenever the feed goes quiet: on 27-Nov-2026 nonprod had loaded
    90 seconds earlier and held nothing newer than 5-Nov.

    Capped at ``reference_date``: a creation date in the future is a data
    fault, and coverage running past today would read as a promise.

    Run once per snapshot build, never per request. ``MAX`` over these views
    is a full scan -- 815,335 EKPO rows on the production copy -- and this
    number is shown on every I08 screen.
    """
    newest: date | None = None
    for view, column in ACTIVITY_DATES:
        try:
            value = db.execute(text(f"select max({column}) from {view}")).scalar()
        except SQLAlchemyError:
            # One unreadable view must not cost the whole watermark: the others
            # still bound it, and a missing view is the normalise step's problem.
            logger.warning("I08 coverage: could not read max(%s) from %s", column, view)
            continue
        if value is None:
            continue
        if isinstance(value, datetime):
            value = value.date()
        if value > reference_date:
            continue
        if newest is None or value > newest:
            newest = value
    return newest


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

    acquisitions: tuple[NewAcquisition, ...] = ()
    """New 80-series purchases, for the Justifications screen's Purchase
    column: the new-buy PRs and the direct PO lines with no PR behind them
    (since 27-Oct-2026; acquisitions.py). Built with the register, so it shares
    its lifetime."""

    source_fingerprint: str = ""
    """What :data:`SOURCE_TABLES` looked like when this build started. A
    different one on a later check means the raw layer moved underneath."""

    source_loaded_at: datetime | None = None
    """When the newest of those tables last loaded successfully."""

    source_oldest_loaded_at: datetime | None = None
    """When the OLDEST of them did -- what a screen may show. See
    :attr:`SourceState.oldest_loaded_at` for why it is not the newest."""

    source_tables: tuple[TableLoad, ...] = ()
    """Every source table and its last load, oldest first, so a screen can
    name the one that is behind."""

    coverage_to: date | None = None
    """The newest activity in the data (:func:`activity_coverage`). None when
    no activity view could be read."""

    repair_requests: tuple[RepairRequest, ...] = ()
    """Open repair PRs with no PO yet (requisitions.py), ordered by PR date.
    Kept apart from ``lines`` on purpose: every reader of ``lines`` -- the
    declaration queue, the assistant -- means PO lines, and a repair with no
    PO yet is served only where it is asked for. Always built;
    ``I8_REPAIR_REQUESTS_ENABLED`` decides only whether it is served."""

    repair_references: tuple[tuple[str, str, str, str], ...] = ()
    """(BANFN, BNFPO, session_id, reference): the assistant reference typed on
    a repair order's 543 line, resolved to a session for the same part
    (repair_references.py, 27-Oct-2026). How a declaration made in that visit
    covers the repair PR and the PO ordered from it."""

    session_jobs: tuple[SessionJob, ...] = ()
    """The job reservation lines whose text names an assistant session, with
    their 261 issues and 262 returns (session_jobs.py, 27-Oct-2026). Served on
    the session page; nothing else reads them."""

    def line(self, document: str, item: str) -> RepairLine | None:
        """One repair line by its (EBELN, EBELP) key."""
        return self._lines_by_key.get((document, item))

    def request(self, purchase_requisition: str, item: str) -> RepairRequest | None:
        """One open repair PR by its (BANFN, BNFPO) key."""
        cached = self.__dict__.get("_request_index")
        if cached is None:
            cached = {request.key: request for request in self.repair_requests}
            object.__setattr__(self, "_request_index", cached)
        return cached.get((purchase_requisition, item))

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
    db: Session,
    cfg: I8Settings | None = None,
    *,
    today: date | None = None,
    staged: bool | None = None,
) -> Snapshot:
    """Assemble the register, the universe and the new purchases.

    Always does the work. :func:`get_snapshot` is the cached entry point.

    No vendor turnaround since 19-Oct-2026: it was dispatch to goods receipt,
    and the register no longer reads goods receipts or lists closed lines.

    ``staged`` (default: ``I8_SNAPSHOT_STAGED``, on) reads narrowed copies of the
    sources on SQL Server instead of the views over the whole extract -- see
    ``staged.py``. Off reads the views directly, as before; the parity test
    builds both ways.
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
    # Here rather than in the status endpoint: a MAX over the activity views is
    # a full scan, and the chip that shows this renders on every I08 screen.
    with timed_step("activity coverage"):
        coverage = activity_coverage(db, reference_date=reference_date)

    settings = get_settings()
    staged = settings.i8_snapshot_staged if staged is None else staged
    with statement_timeout(db, settings.i8_snapshot_statement_timeout_seconds):
        with staged_sources(db, cfg, enabled=staged) as sources:
            # One EKPO pull for both readings of it: repair lines and new purchases.
            with timed_step("PO lines (EKPO)"):
                candidates = fetch_candidate_lines(db, sources=sources)
            lines, register_stats = load_repair_lines(
                db, cfg, today=reference_date, candidates=candidates, sources=sources
            )
            # The repair orders' lines, read once: they name the open repair
            # PRs, and they tell a repair PR from a new-buy one.
            reservation_lines = fetch_repair_order_lines(db, cfg, sources=sources)
            # Before the EKPO pull is let go: a PR is open only while no live
            # PO line points back to it, and those lines are in this pull.
            # The same EBAN read also gives every PO line its PR's date and
            # department (27-Oct-2026): the queue's requester is the
            # department that asked for the repair.
            requisitions: dict = {}
            repair_requests, request_stats = load_repair_requests(
                db,
                cfg,
                reference_date=reference_date,
                candidates=candidates,
                sources=sources,
                reservation_lines=reservation_lines,
                also_read=(line.pr_number for line in lines if line.pr_number),
                requisitions_out=requisitions,
            )
            lines = with_requisition_details(lines, requisitions)
            del requisitions
            # The new-buy PRs, and the direct PO lines in the same pull.
            acquisitions = load_new_acquisitions(
                db, cfg, candidates=candidates, reservation_lines=reservation_lines, sources=sources
            )
            # Before the lines are let go: their text carries the R-reference.
            repair_references = load_repair_references(db, reservation_lines, cfg)
            del reservation_lines
            # The job each assistant session was for: its own reservation line.
            session_jobs = load_session_jobs(db, cfg, sources=sources)
            register_stats = replace(
                register_stats,
                open_repair_requests=request_stats.open_repair_requests,
                requisitions_excluded_deleted=request_stats.requisitions_excluded_deleted,
                requisitions_excluded_live_po=request_stats.requisitions_excluded_live_po,
                requisitions_excluded_reservation_closed=request_stats.requisitions_excluded_reservation_closed,
            )
            del candidates
            with timed_step("repairable universe"):
                universe, universe_stats = load_universe(
                    db, cfg, open_repair_index=open_repair_index(lines), sources=sources
                )

    elapsed = time.monotonic() - started
    logger.info(
        "I08 snapshot built in %.2fs: %d open repair lines, %d open repair PRs, %d universe rows",
        elapsed,
        len(lines),
        len(repair_requests),
        len(universe),
    )
    return Snapshot(
        reference_date=reference_date,
        built_at=datetime.now(timezone.utc),
        build_seconds=round(elapsed, 2),
        lines=tuple(lines),
        register_stats=register_stats,
        universe=tuple(universe),
        universe_stats=universe_stats,
        acquisitions=tuple(acquisitions),
        source_fingerprint=source.fingerprint,
        source_loaded_at=source.loaded_at,
        source_oldest_loaded_at=source.oldest_loaded_at,
        source_tables=source.per_table,
        coverage_to=coverage,
        repair_requests=repair_requests,
        repair_references=repair_references,
        session_jobs=session_jobs,
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
    last_check: float | None = None
    """``time.monotonic()`` of the last fingerprint check -- see
    :func:`check_source_fingerprint`, which is rate-limited by it.

    None for "never checked", NOT 0.0. ``time.monotonic()`` counts from boot
    on Linux, so against a 0.0 default the very first check reads as one that
    happened ``uptime`` seconds ago -- and is skipped on any machine up for
    less than the interval. That is every fresh container and every CI runner,
    and it does not reproduce on a developer machine that has been awake for
    days, which is exactly how it got here."""


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


def _build_locked(
    db: Session, cfg: I8Settings | None, reason: str, *, rebuild: bool = False
) -> Snapshot:
    """Build and record the outcome. The caller holds ``_build_lock``.

    With ``I8_SNAPSHOT_STORE=sql`` nothing is built here: the newest stored
    snapshot is loaded, after a build PROCESS when ``rebuild`` or when none is
    stored yet (``snapshot_store/lifecycle.py``). The web worker only waits.
    """
    from app.initiatives.i8.snapshot_store import lifecycle as store_lifecycle

    with _state_lock:
        _state.building_since = datetime.now(timezone.utc)
    logger.info("I08 snapshot build started (%s)", reason)
    try:
        if store_lifecycle.enabled():
            snapshot = store_lifecycle.load_or_build(db, reason, rebuild=rebuild)
        else:
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

    # The two caches BUILT ON TOP of this one cannot outlive it.
    #
    # This used to be missing, and it is the whole of audit finding 11.2. The
    # attestation view is rebuilt only when somebody writes a declaration, and
    # the coding screen only on restart -- so after a SAP reload the register
    # showed new repair lines beside a declaration column computed from the
    # data before it.
    #
    # Here rather than in reset_snapshot(): THIS is the line that makes a new
    # snapshot live, and the reload path never calls reset_snapshot() at all.
    # Anything derived has to be dropped at the same instant, or there is a
    # window where the two disagree.
    reset_attestation_view()
    reset_coding_screen()
    _schedule_coding_prewarm(snapshot)
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
        return _build_locked(db, cfg, "refresh" if refresh else "first request", rebuild=refresh)
    finally:
        _build_lock.release()


def cached_snapshot() -> Snapshot | None:
    """The snapshot already built, or None. Never builds and never waits.

    For a caller that can do without it -- a label, not the data it serves.
    """
    with _state_lock:
        return _state.snapshot


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
            _build_locked(db, None, reason, rebuild=force)
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
        _state.last_check = None
    # The attestation view and the coding screen are both derived from the
    # snapshot, so neither can outlive it. _build_locked does the same thing
    # when it swaps a NEW snapshot in; this is the drop-to-nothing case.
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
        checked = _state.last_check
        if snapshot is None or (checked is not None and time.monotonic() - checked < interval):
            return False
        _state.last_check = time.monotonic()

    from app.initiatives.i8.snapshot_store import lifecycle as store_lifecycle

    if store_lifecycle.enabled():
        # A version another process stored (a build run by hand, the other
        # container during a restart) replaces this one without a build here.
        newer = store_lifecycle.newer_stored(db, snapshot.built_at)
        if newer is not None:
            with _state_lock:
                _state.snapshot = snapshot = newer

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


@dataclass(frozen=True)
class Freshness:
    """How current the data on an I08 screen is.

    Answerable WITHOUT a built snapshot, which is the whole point of it: the
    screens ask on every page view, and ``GET /i8/snapshot`` cannot serve them
    because it takes the snapshot as a dependency and 503s for the 90-odd
    seconds a build takes. That is exactly when somebody wants to know.
    """

    reference_date: date
    """The date the screens measure ages against."""

    loaded_at: datetime | None
    """The OLDEST source load behind what is on screen: everything is at
    least this fresh. Never the newest -- see
    :attr:`SourceState.oldest_loaded_at`."""

    newest_loaded_at: datetime | None
    """The newest source load, for the detail. Equal to :attr:`loaded_at`
    when every table loaded together, which is the healthy case."""

    tables: tuple[TableLoad, ...]
    """Every source table, oldest load first."""

    coverage_to: date | None
    """The newest activity the data holds. None while no snapshot has been
    built, because it is measured during the build."""

    built_at: datetime | None
    """When the snapshot on screen was built. None while the first one is
    still building."""

    building_since: datetime | None
    """Set while a build is running, so a screen can say so rather than
    showing a stale time with no explanation."""


def freshness(db: Session) -> Freshness:
    """What to tell the user about how current the screens are.

    Load times come from the snapshot being served, not from a live read of
    ``ingestion_run``: the question is how fresh THIS data is, and a raw layer
    that has moved since the build describes data nobody is looking at yet.
    The live read is the fallback only while no snapshot exists, so the chip
    says something during a first build instead of nothing.
    """
    cfg = get_i8_settings()
    reference_date = cfg.reference_date_value or date.today()
    with _state_lock:
        snapshot = _state.snapshot
        building_since = _state.building_since

    if snapshot is None:
        live = source_state(db, reference_date=reference_date)
        return Freshness(
            reference_date=reference_date,
            loaded_at=live.oldest_loaded_at,
            newest_loaded_at=live.loaded_at,
            tables=live.per_table,
            coverage_to=None,
            built_at=None,
            building_since=building_since,
        )

    return Freshness(
        reference_date=snapshot.reference_date,
        loaded_at=snapshot.source_oldest_loaded_at,
        newest_loaded_at=snapshot.source_loaded_at,
        tables=snapshot.source_tables,
        coverage_to=snapshot.coverage_to,
        built_at=snapshot.built_at,
        building_since=building_since,
    )


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
# Declarations are written by the assistant (since 3-Nov-2026 the only place),
# and also by `demo_seed` (a separate process -- after `--clear` a running server
# kept serving the demo coverage until it restarted). So every read compares a
# cheap fingerprint of the table -- a count and a latest timestamp, one
# aggregate query -- and rebuilds when it moved. The table is append-only apart
# from the demo clear, which changes the count.


@dataclass(frozen=True)
class AttestationView:
    """Coverage and the declaration queue, built together.

    One object because they are two readings of a single pass: which repair
    lines have an attestation. Building them separately would mean matching
    every line against the attestation table twice to get two answers that
    must agree.
    """

    coverage: AttestationCoverage
    declarations: tuple[DeclarationRow, ...]
    built_at: datetime
    source_fingerprint: tuple | None = None
    """What the attestation table looked like when this was built. A
    different fingerprint on read means another writer moved it."""

    @property
    def declaration_by_line(self) -> dict[tuple[str, str], DeclarationRow]:
        """(document, item) -> the line's declaration-queue row, for the register.

        The register carries the declaration status, and since the Declaration
        Queue screen was folded into it (08-Oct-2026) also who declared, when,
        the condition and the next action -- all of which the queue row already
        holds. Memoised on the instance because the register maps 1,225 rows
        and rebuilding the index per row would be the per-row cost this whole
        view exists to avoid.
        """
        cached = self.__dict__.get("_declaration_index")
        if cached is None:
            cached = {
                # DeclarationRow.related_repair_id is "{document}-{item}", the
                # same key the register uses, but the line's own tuple is what
                # the caller holds -- so it is rebuilt from the id's parts.
                tuple(row.related_repair_id.rsplit("-", 1)): row
                for row in self.declarations
            }
            object.__setattr__(self, "_declaration_index", cached)
        return cached


def source_fingerprint(db: Session) -> tuple:
    """A cheap summary of the table the attestation view is derived from:
    the attestation table's count and latest timestamp. One aggregate query,
    portable across Postgres and Azure SQL."""
    from app.initiatives.i8.models import RepairAttestation

    attestations = db.execute(
        select(func.count(), func.max(RepairAttestation.attested_at))
    ).one()
    return (tuple(attestations),)


def build_attestation_view(
    db: Session,
    snapshot: Snapshot,
    cfg: I8Settings | None = None,
    *,
    fingerprint: tuple | None = None,
) -> AttestationView:
    """Match every repair line against the attestation table. Always does the
    work."""
    cfg = cfg or get_i8_settings()
    # The open repair PRs are declared on only while the register serves them.
    requests = snapshot.repair_requests if cfg.repair_requests_enabled else ()
    cover = attestation_coverage(
        db,
        snapshot.lines,
        cfg,
        requests=requests,
        references=getattr(snapshot, "repair_references", ()),
    )
    declarations = build_queue(
        snapshot.lines,
        cover,
        requests=requests,
        cutover=cfg.attestation_cutover_date_value,
    )
    return AttestationView(
        coverage=cover,
        declarations=tuple(declarations),
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

    Explicit rather than automatic, and called at the write site, so that the
    one place that changes the data is also the place that says so. A TTL
    would be the alternative and would mean a planner's own submission takes up
    to N seconds to appear, which is indistinguishable from broken.
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
    """False when no model was CALLED in this build.

    Not the same as "nothing was judged": since 14-Oct-2026 a deterministic
    build still serves verdicts already on record, so read
    :attr:`ScreenStats.verdicts_reused` for that, not this flag."""

    verdict_stamp: tuple[int, datetime | None] | None = None
    """What the verdict table held when this screen was built -- see
    :func:`app.initiatives.i8.coding_store.verdict_stamp`.

    The cache above is per PROCESS and the verdicts are shared, so this is how
    a worker notices that the OTHER one judged something since. ``None`` means
    the stamp could not be read, and a screen carrying it is never considered
    stale: it is served as it is.

    Defaulted rather than required so a test can build a screen without one."""


ScreenKey = tuple[bool, int | None, tuple[str, ...]]

_screen_lock = threading.Lock()
#: Keyed by (use_model, limit, sources) so a fast run and a full run do not
#: evict each other -- a demo typically wants both -- and so a run over one set
#: of free-text sources is never served for a request that asked for another.
#: The sources belong in the key for correctness, not convenience: the whole
#: point of asking for PO,PR,MOVEMENT is to compare it against PO,PR, and a
#: cache that ignored them would answer both questions with one number.
_screens: dict[ScreenKey, CodingScreen] = {}


def build_coding_screen(
    db: Session,
    snapshot: Snapshot,
    cfg: I8Settings | None = None,
    *,
    use_model: bool = False,
    limit: int | None = None,
    sources: tuple[str, ...] | None = None,
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
        sources=sources,
    )
    return CodingScreen(
        candidates=tuple(candidates),
        stats=stats,
        built_at=datetime.now(timezone.utc),
        build_seconds=round(time.monotonic() - started, 2),
        used_model=use_model,
        # Read AFTER the run, which may have stored verdicts of its own: the
        # stamp has to describe the table this screen already reflects, or the
        # very next request would read it back as stale.
        verdict_stamp=read_verdict_stamp(db),
    )


def _refreshed(db: Session, key: ScreenKey, cached: CodingScreen) -> CodingScreen:
    """``cached``, with the verdicts the table holds now. The caller holds the lock.

    Dropping the entry and rebuilding would be simpler and much slower: the
    rebuild re-stages the sources (10-24 s on live data) to recompute facts
    that cannot have changed. Only the judgement is re-read.

    Without a session there is nothing to compare against, so the cached screen
    is returned untouched -- the same answer this gave before the check existed.
    """
    if db is None:
        return cached

    stamp = read_verdict_stamp(db)
    if stamp is None or stamp == cached.verdict_stamp:
        return cached

    candidates, stats = reapply_stored_verdicts(db, cached.candidates, cached.stats)
    refreshed = replace(
        cached, candidates=tuple(candidates), stats=stats, verdict_stamp=stamp
    )
    _screens[key] = refreshed
    logger.info(
        "I08 coding screen: the verdict table moved (%s -> %s), so the cached "
        "screen was re-read: %d of %d candidates now carry a verdict",
        cached.verdict_stamp,
        stamp,
        stats.verdicts_reused,
        len(candidates),
    )
    return refreshed


def get_coding_screen(
    db: Session,
    snapshot: Snapshot,
    cfg: I8Settings | None = None,
    *,
    use_model: bool = False,
    limit: int | None = None,
    sources: tuple[str, ...] | None = None,
    refresh: bool = False,
) -> CodingScreen:
    """The cached screen for this (use_model, limit, sources), built on first use.

    A run that STORES verdicts invalidates every cached fast screen, because
    those were built when the materials it just judged had no verdict on
    record and would go on reporting them UNSCREENED. That is what made a
    screen look like it had been thrown away on leaving the page: the verdicts
    were in the database and the cache in front of it was older than they were.

    That drop reaches only THIS process, and prod runs more than one worker.
    The worker that ran the model cleared its own stale screens; the other one
    went on serving a list built before those verdicts existed, so a refresh
    showed the new verdicts or the old UNSCREENED rows depending on which
    worker answered (seen on prod, 6-Nov-2026). A cache hit is therefore
    checked against the verdict table -- shared by every worker -- and re-read
    when it has moved. See :func:`_refreshed`.
    """
    cfg = cfg or get_i8_settings()
    # Resolved here rather than left as None, so the key names the sources the
    # run actually read. Two requests that resolve to the same sources -- one
    # naming them, one taking the default -- are the same run and should share
    # one cache entry.
    resolved = tuple(sources) if sources is not None else cfg.coding_candidate_source_list
    key = (use_model, limit, resolved)
    with _screen_lock:
        if refresh or key not in _screens:
            built = build_coding_screen(
                db, snapshot, cfg, use_model=use_model, limit=limit, sources=resolved
            )
            if built.stats.verdicts_stored:
                # Only the fast screens, and only the ones that are not this
                # build: a model run's own entry is the newest thing here.
                # Dropped rather than rebuilt -- the next page load rebuilds
                # one in about a second, and rebuilding every variant here
                # would make a batch pay for screens nobody asked for.
                stale = [k for k in _screens if k[0] is False]
                for k in stale:
                    del _screens[k]
                if stale:
                    logger.info(
                        "I08 coding screen: %d verdict(s) stored, so %d cached "
                        "fast screen(s) were dropped as stale",
                        built.stats.verdicts_stored,
                        len(stale),
                    )
            if built.stats.new_item_price_counts.get(MissingPrice.LOOKUP_FAILED.value):
                # Served but not kept: cached, a screen without its prices
                # would stay that way until the next snapshot. Uncached, the
                # next page load asks MBEW again.
                return built
            _screens[key] = built
            return built
        return _refreshed(db, key, _screens[key])


def peek_unscreened_count(
    cfg: I8Settings | None = None, *, db: Session | None = None
) -> int | None:
    """How many candidates the cached fast screen holds with no verdict yet --
    the "not screened yet" rows the Repair Candidates page offers to the AI
    screen -- or None when that screen is not cached (4-Nov-2026).

    Never builds it: the menu's badge asks on every page. The fast screen the
    page loads is the one keyed (no model, no limit, the default sources).

    ``db`` is checked against the verdict table first, so the badge cannot
    disagree with the page it points at -- it reads the same cache, and that
    cache can have been left behind by another worker's model run. Optional
    because the check is the only thing here that needs a database; without one
    the cached count is served as it stands.
    """
    cfg = cfg or get_i8_settings()
    key: ScreenKey = (False, None, cfg.coding_candidate_source_list)
    # Not `with _screen_lock`: a screen being built holds the lock for ~10 s, and
    # this is asked on every page. Busy means "not ready", which is None.
    if not _screen_lock.acquire(blocking=False):
        return None
    try:
        screen = _screens.get(key)
        if screen is not None and db is not None:
            screen = _refreshed(db, key, screen)
    finally:
        _screen_lock.release()
    if screen is None:
        return None
    return sum(1 for candidate in screen.candidates if candidate.verdict == Verdict.UNSCREENED.value)


def reset_coding_screen() -> None:
    """Drop every cached screen. For tests, and for a deliberate re-run."""
    with _screen_lock:
        _screens.clear()


def _schedule_coding_prewarm(snapshot: Snapshot) -> None:
    """Build the Repair Candidates screen a little after ``snapshot`` goes live.

    A swap drops the cached screen (see :func:`_build_locked`), so the first
    open of the page after every rebuild paid the whole ~10 s build, and the
    menu's badge -- which only reads a built screen -- showed nothing until
    somebody opened it. This builds the one the page and the badge read, in the
    background, :attr:`I8Settings.coding_prewarm_delay_seconds` later.
    """
    cfg = get_i8_settings()
    if not cfg.coding_prewarm_enabled:
        return
    timer = threading.Timer(max(cfg.coding_prewarm_delay_seconds, 0.0), _prewarm_coding_screen, args=(snapshot,))
    timer.daemon = True
    timer.name = "i8-coding-prewarm"
    timer.start()


def _prewarm_coding_screen(snapshot: Snapshot) -> None:
    """The default screen (fast, no limit, default sources: the key the page and
    the badge use), built with its own session.

    Skipped when another snapshot has replaced this one meanwhile -- that one
    schedules its own. Never raises: a failure is logged and the page builds
    the screen on first open, as it always did.
    """
    with _state_lock:
        if _state.snapshot is not snapshot:
            return
    db = get_sessionmaker()()
    try:
        started = time.monotonic()
        get_coding_screen(db, snapshot)
        logger.info("I08 coding candidates: screen ready in %.1fs", time.monotonic() - started)
    except Exception:
        logger.exception("I08 coding candidates: the background build failed; the page builds it on first open")
    finally:
        db.close()

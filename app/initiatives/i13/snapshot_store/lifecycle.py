"""When the stored snapshot is built, and what is served meanwhile.

``snapshot.py``'s public functions -- ``start_background_build``,
``check_fingerprint``, ``snapshot_status``, ``refresh_material`` -- hand over to
this module when ``I13_SNAPSHOT_STORE=sql``, so ``app.main`` and the callers
elsewhere do not change.

How a build runs
----------------
**As its own process** (``python -m app.initiatives.i13.snapshot_store build``),
never as a thread of the web worker. Inside the worker a heavy batch could take
the worker's memory or stall its heartbeat past gunicorn's 120 s timeout, and
gunicorn then killed the worker: the site went down and took the build with it.

**Under a database lock** (``build_lock``). Only the process holding it may
build, and start-up clean-up only touches builds when it can take the lock
itself -- so a running build, in this process or another container, is never
cleaned up from under it. A killed build releases the lock with its connection.

Who starts a build
------------------
Start-up (when nothing current is stored), the data-change watcher, and a manual
``POST /api/i13/snapshot/refresh``. **A page request never does**: before this,
a request finding no version started one, which raced start-up's clean-up --
the clean-up then deleted the new build's tables, and with the frontend
retrying it happened again and again (48 failed builds overnight on 10-11 Oct).

Automatic attempts at the same data stop after ``I13_BUILD_MAX_ATTEMPTS`` in an
hour, so a build that fails the same way every time does not re-copy the large
tables forever; the error stays in ``GET /api/i13/snapshot`` and a manual
refresh still runs.

Otherwise as before: a restart serves the stored version without rebuilding;
a data pull is waited out (no open CSV request, no load in the last
``_SETTLE_SECONDS``); the previous version serves until a new one is complete.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.models import IngestionRun
from app.models.csv_extract import STATUS_OPEN, CsvExtractRequest
from app.models.i13_snapshot_store import I13SnapshotRun

logger = get_logger(__name__)

#: A load newer than this is taken to be part of a pull still running.
_SETTLE_SECONDS = 600
#: The window I13_BUILD_MAX_ATTEMPTS counts over.
_ATTEMPT_WINDOW = timedelta(hours=1)
#: Where `python -m app...` runs from: the directory holding the `app` package.
_APP_ROOT = Path(__file__).resolve().parents[4]


def enabled() -> bool:
    return get_settings().i13_snapshot_store.strip().lower() == "sql"


@dataclass
class _State:
    #: The thread that launched, and waits for, this process's build process.
    thread: threading.Thread | None = None
    process: subprocess.Popen | None = None
    building_since: datetime | None = None
    last_error: str | None = None
    last_check: float = 0.0


_state = _State()
_lock = threading.Lock()


def _building_here() -> bool:
    return _state.thread is not None and _state.thread.is_alive()


def _lock_held() -> bool:
    from app.initiatives.i13.snapshot_store import build_lock

    try:
        return build_lock.held()
    except Exception:
        logger.warning("I13 store: could not test the build lock", exc_info=True)
        return False


def building() -> bool:
    """A build is running -- launched here, or by any other process."""
    with _lock:
        if _building_here():
            return True
    return _lock_held()


def ready_run(db: Session) -> I13SnapshotRun | None:
    from app.initiatives.i13.snapshot_store import kinds

    run = db.execute(
        select(I13SnapshotRun)
        .where(I13SnapshotRun.status == "ready")
        .order_by(I13SnapshotRun.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if run is not None and run.schema_signature != kinds.signature():
        # Written by code with a different item layout: never decode it.
        return None
    return run


def _latest_run(db: Session) -> I13SnapshotRun | None:
    return db.execute(select(I13SnapshotRun).order_by(I13SnapshotRun.version.desc()).limit(1)).scalar_one_or_none()


def _last_failure(db: Session, running: bool) -> str | None:
    """The newest run's error, if it failed -- or was interrupted: still marked
    ``building`` with no build running anywhere."""
    latest = _latest_run(db)
    if latest is None or latest.status == "ready":
        return None
    if latest.status == "failed":
        return latest.error
    if latest.status == "building" and not running:
        return f"interrupted: build v{latest.version} stopped before it finished"
    return None


def current(db: Session):
    """The served version as a ``SqlSnapshot``.

    Raises ``SnapshotBuilding`` while a first version is being built and
    ``SnapshotFailed`` when there is none and none is building -- the same
    contract as ``snapshot.get_i13_snapshot``, so ``deps.require_snapshot`` maps
    both to the same 503s. **Never starts a build**: see the module docstring.
    """
    from app.initiatives.i13.snapshot import SnapshotBuilding, SnapshotFailed
    from app.initiatives.i13.snapshot_store.reader import SqlSnapshot

    run = ready_run(db)
    if run is not None:
        return SqlSnapshot(db, run)
    if building():
        latest = _latest_run(db)
        since = _state.building_since or (latest.started_at if latest is not None else None)
        raise SnapshotBuilding(since)
    raise SnapshotFailed(
        _state.last_error
        or _last_failure(db, running=False)
        or "no I13 snapshot has been built yet; it builds at start-up and when new data loads "
        "(POST /api/i13/snapshot/refresh starts one now)"
    )


# --- starting a build ----------------------------------------------------------


def _attempts_left(db: Session) -> int:
    """Automatic attempts left at the current data (same fingerprint)."""
    from app.initiatives.i13.snapshot import compute_fingerprint

    fingerprint = compute_fingerprint(db)
    since = datetime.now(timezone.utc) - _ATTEMPT_WINDOW
    tried = db.execute(
        select(func.count())
        .select_from(I13SnapshotRun)
        .where(
            I13SnapshotRun.fingerprint == fingerprint,
            I13SnapshotRun.status != "ready",
            I13SnapshotRun.started_at >= since,
        )
    ).scalar_one()
    return max(get_settings().i13_build_max_attempts - tried, 0)


def _command(reason: str) -> list[str]:
    return [sys.executable, "-m", "app.initiatives.i13.snapshot_store", "build", "--reason", reason]


def _launch_and_wait(reason: str, after: threading.Thread | None, manual: bool) -> None:
    if after is not None:
        after.join()
    try:
        if _lock_held():
            logger.info("I13 store: a build is already running elsewhere; not starting another (%s)", reason)
            return
        if not manual:
            db = get_sessionmaker()()
            try:
                left = _attempts_left(db)
            finally:
                db.close()
            if left <= 0:
                logger.warning(
                    "I13 store: %d automatic attempts at this data in the last hour all failed; "
                    "not retrying (%s). See GET /api/i13/snapshot; POST /api/i13/snapshot/refresh to force one.",
                    get_settings().i13_build_max_attempts,
                    reason,
                )
                return
        logger.info("I13 store: starting a build process (%s)", reason)
        process = subprocess.Popen(
            _command(reason), cwd=_APP_ROOT, env=os.environ.copy(), stdin=subprocess.DEVNULL
        )
        with _lock:
            _state.process = process
        code = process.wait()
        error = None
        if code not in (0, 3):
            db = get_sessionmaker()()
            try:
                error = _last_failure(db, running=False) or f"build process exited with code {code}"
            finally:
                db.close()
        with _lock:
            _state.last_error = error
            _state.last_check = time.monotonic()
        logger.info("I13 store: build process finished with code %s (%s)", code, reason)
    except Exception as exc:
        logger.exception("I13 store: could not run a build (%s)", reason)
        with _lock:
            _state.last_error = f"{type(exc).__name__}: {exc}"
    finally:
        with _lock:
            _state.process = None
            _state.building_since = None


def start_background_build(reason: str, *, after: threading.Thread | None = None, manual: bool = False) -> bool:
    """Launch a build process from a daemon thread that waits for it. False if
    this process already has one running. ``manual`` skips the attempt limit."""
    with _lock:
        if _building_here():
            return False
        _state.building_since = datetime.now(timezone.utc)
        thread = threading.Thread(
            target=_launch_and_wait, args=(reason, after, manual), name="i13-store-build", daemon=True
        )
        _state.thread = thread
    thread.start()
    return True


def _pull_in_progress(db: Session) -> str | None:
    """Why a rebuild should wait, or ``None``."""
    open_requests = db.execute(
        select(func.count()).select_from(CsvExtractRequest).where(CsvExtractRequest.status == STATUS_OPEN)
    ).scalar_one()
    if open_requests:
        return f"{open_requests} CSV extract request(s) still open"
    latest = db.execute(select(func.max(IngestionRun.finished_at))).scalar()
    if latest is not None:
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - latest < timedelta(seconds=_SETTLE_SECONDS):
            return f"a load finished at {latest.isoformat()}, less than {_SETTLE_SECONDS}s ago"
    return None


def start_up(after: threading.Thread | None = None) -> None:
    """Clear builds that died (only while no build is running anywhere), then
    build if nothing current is stored."""
    from app.initiatives.i13.snapshot import compute_fingerprint
    from app.initiatives.i13.snapshot_store import builder
    from app.initiatives.i13.snapshot_store.build_lock import BuildLock

    if after is not None:
        after.join()
    db = get_sessionmaker()()
    try:
        lock = BuildLock()
        if not lock.acquire():
            logger.info("I13 store: a build is running in another process; start-up leaves it alone")
            return
        try:
            abandoned = builder.abandon_unfinished(db)
            if abandoned:
                logger.info("I13 store: cleared %d build(s) that stopped before finishing", abandoned)
        finally:
            lock.release()
        run = ready_run(db)
        if run is not None and run.fingerprint == compute_fingerprint(db):
            logger.info("I13 store: serving v%s (built %s); no build needed", run.version, run.built_at)
            with _lock:
                _state.last_check = time.monotonic()
            return
        waiting = _pull_in_progress(db) if run is not None else None
    finally:
        db.close()
    if waiting:
        logger.info("I13 store: data changed but a pull is running (%s); serving the stored version", waiting)
        return
    start_background_build("start-up")


def too_soon(built_at: datetime | None, now: datetime, gap_minutes: int) -> timedelta | None:
    """How much longer the rebuild gap holds, or None when a build may start.

    Pure, for the tests. ``built_at`` is when the served version was built;
    a store with nothing served has no gap to keep.
    """
    if built_at is None or gap_minutes <= 0:
        return None
    if built_at.tzinfo is None:
        built_at = built_at.replace(tzinfo=timezone.utc)
    remaining = built_at + timedelta(minutes=gap_minutes) - now
    return remaining if remaining > timedelta(0) else None


def check_fingerprint(db: Session, *, min_interval_seconds: float | None = None) -> bool:
    from app.initiatives.i13.snapshot import compute_fingerprint

    settings = get_settings()
    interval = settings.i13_snapshot_check_interval_seconds if min_interval_seconds is None else min_interval_seconds
    with _lock:
        if _building_here() or time.monotonic() - _state.last_check < interval:
            return False
        _state.last_check = time.monotonic()
    if _lock_held():
        return False
    run = ready_run(db)
    if run is not None and run.fingerprint == compute_fingerprint(db):
        return False
    waiting = _pull_in_progress(db)
    if waiting:
        logger.info("I13 store: data changed; waiting for the pull to finish (%s)", waiting)
        return False
    remaining = too_soon(
        run.built_at if run is not None else None, datetime.now(timezone.utc), settings.i13_rebuild_min_gap_minutes
    )
    if remaining is not None:
        # Automatic builds only: a manual refresh goes through
        # start_background_build directly and is not held back.
        logger.info(
            "I13 store: data changed; serving v%s until the rebuild gap passes (%d min left of %d)",
            run.version, int(remaining.total_seconds() // 60) + 1, settings.i13_rebuild_min_gap_minutes,  # type: ignore[union-attr]
        )
        return False
    return start_background_build("data changed")


# --- what is being served ------------------------------------------------------

_served_cache: tuple[float, dict[str, Any] | None] = (0.0, None)
_SERVED_TTL_SECONDS = 30.0


def served_info() -> dict[str, Any] | None:
    """built_at / reference_date / rebuilding of the served version, for the
    response headers -- cached briefly so they are not a query per request."""
    global _served_cache
    fetched_at, info = _served_cache
    if time.monotonic() - fetched_at < _SERVED_TTL_SECONDS:
        return info
    db = get_sessionmaker()()
    try:
        run = ready_run(db)
    except Exception:
        logger.warning("I13 store: could not read the served version for the response headers", exc_info=True)
        run = None
    finally:
        db.close()
    info = (
        {"built_at": run.built_at, "reference_date": run.reference_date, "rebuilding": building()}
        if run is not None and run.built_at is not None
        else None
    )
    _served_cache = (time.monotonic(), info)
    return info


def status() -> dict[str, Any]:
    running = building()
    db = get_sessionmaker()()
    try:
        run = ready_run(db)
        latest = _latest_run(db)
        error = _state.last_error or _last_failure(db, running=running)
    finally:
        db.close()
    since = _state.building_since or (latest.started_at if running and latest is not None else None)
    return {
        "status": "building" if running else ("ready" if run is not None else ("failed" if error else "idle")),
        "enabled": get_settings().i13_snapshot_enabled,
        "version": run.version if run else None,
        "reference_date": run.reference_date if run else None,
        "built_at": run.built_at if run else None,
        "build_seconds": float(run.build_seconds) if run and run.build_seconds is not None else None,
        "fingerprint": run.fingerprint if run else None,
        "building_since": since if running else None,
        "rebuilding": running and run is not None,
        "last_error": error,
    }

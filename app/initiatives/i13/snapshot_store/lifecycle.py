"""When the stored snapshot is built, and what is served meanwhile.

``snapshot.py``'s public functions -- ``start_background_build``,
``check_fingerprint``, ``snapshot_status``, ``refresh_material`` -- hand over to
this module when ``I13_SNAPSHOT_STORE=sql``, so ``app.main`` and the callers
elsewhere do not change.

Differences from the in-memory lifecycle, all because the snapshot now
outlives the process:

* **Start-up does not always build.** A ready version whose fingerprint still
  matches the data is served as it is; a restart costs nothing. Only a missing
  or stale version starts a build, and a build a restart interrupted is
  cleared first (``builder.abandon_unfinished``).
* **A data pull is waited out.** A CSV full refresh replaces ~24 tables over a
  few hours, each changing the fingerprint. Rebuilding on every one would
  build against half-loaded data, over and over. A change is acted on only
  once no CSV extract request is open and no load has finished in the last
  ``_SETTLE_SECONDS``.
* **The previous version serves throughout.** Routes answer 503 only when no
  version has ever been built.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.models import IngestionRun
from app.models.csv_extract import STATUS_OPEN, CsvExtractRequest
from app.models.i13_snapshot_store import I13SnapshotRun
from app.shared.snapshot_builds import exclusive_build

logger = get_logger(__name__)

#: A load newer than this is taken to be part of a pull still running.
_SETTLE_SECONDS = 600
_RETRY_AFTER_FAILURE_SECONDS = 300.0


def enabled() -> bool:
    return get_settings().i13_snapshot_store.strip().lower() == "sql"


@dataclass
class _State:
    thread: threading.Thread | None = None
    building_since: datetime | None = None
    last_error: str | None = None
    last_check: float = 0.0
    failed_at: float = 0.0


_state = _State()
_lock = threading.Lock()


def _building() -> bool:
    return _state.thread is not None and _state.thread.is_alive()


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


def current(db: Session):
    """The served version as a ``SqlSnapshot``.

    Raises ``SnapshotBuilding`` while the first version is being built and
    ``SnapshotFailed`` when none could be -- the same contract as
    ``snapshot.get_i13_snapshot``, so ``deps.require_snapshot`` maps both to
    the same 503s.
    """
    from app.initiatives.i13.snapshot import SnapshotBuilding, SnapshotFailed
    from app.initiatives.i13.snapshot_store.reader import SqlSnapshot

    run = ready_run(db)
    if run is not None:
        return SqlSnapshot(db, run)
    with _lock:
        building, since, error = _building(), _state.building_since, _state.last_error
        failed_ago = time.monotonic() - _state.failed_at
    if building:
        raise SnapshotBuilding(since)
    if error is not None and failed_ago < _RETRY_AFTER_FAILURE_SECONDS:
        raise SnapshotFailed(error)
    start_background_build("no stored snapshot")
    raise SnapshotBuilding(datetime.now(timezone.utc))


def _run_build(reason: str, after: threading.Thread | None) -> None:
    if after is not None:
        after.join()
    from app.initiatives.i13.snapshot_store import builder

    db = get_sessionmaker()()
    try:
        with exclusive_build("I13"):
            builder.build(db, reason=reason)
        with _lock:
            _state.last_error = None
            _state.last_check = time.monotonic()
    except Exception as exc:  # noqa: BLE001 -- recorded and surfaced via status
        with _lock:
            _state.last_error = f"{type(exc).__name__}: {exc}"
            _state.failed_at = time.monotonic()
    finally:
        db.close()
        with _lock:
            _state.building_since = None


def start_background_build(reason: str, *, after: threading.Thread | None = None) -> bool:
    with _lock:
        if _building():
            return False
        _state.building_since = datetime.now(timezone.utc)
        thread = threading.Thread(target=_run_build, args=(reason, after), name="i13-store-build", daemon=True)
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
    """Clear interrupted builds; build only if nothing current is stored."""
    from app.initiatives.i13.snapshot import compute_fingerprint
    from app.initiatives.i13.snapshot_store import builder

    db = get_sessionmaker()()
    try:
        abandoned = builder.abandon_unfinished(db)
        if abandoned:
            logger.info("I13 store: cleared %d build(s) a restart interrupted", abandoned)
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
    start_background_build("start-up", after=after)


def check_fingerprint(db: Session, *, min_interval_seconds: float | None = None) -> bool:
    from app.initiatives.i13.snapshot import compute_fingerprint

    interval = (
        get_settings().i13_snapshot_check_interval_seconds if min_interval_seconds is None else min_interval_seconds
    )
    with _lock:
        if _building() or time.monotonic() - _state.last_check < interval:
            return False
        _state.last_check = time.monotonic()
    run = ready_run(db)
    if run is not None and run.fingerprint == compute_fingerprint(db):
        return False
    waiting = _pull_in_progress(db)
    if waiting:
        logger.info("I13 store: data changed; waiting for the pull to finish (%s)", waiting)
        return False
    return start_background_build("data changed")


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
    except Exception:  # noqa: BLE001 -- a header must never fail the response
        logger.warning("I13 store: could not read the served version for the response headers", exc_info=True)
        run = None
    finally:
        db.close()
    with _lock:
        building = _building()
    info = (
        {"built_at": run.built_at, "reference_date": run.reference_date, "rebuilding": building}
        if run is not None and run.built_at is not None
        else None
    )
    _served_cache = (time.monotonic(), info)
    return info


def status() -> dict[str, Any]:
    db = get_sessionmaker()()
    try:
        run = ready_run(db)
        latest = db.execute(
            select(I13SnapshotRun).order_by(I13SnapshotRun.version.desc()).limit(1)
        ).scalar_one_or_none()
    finally:
        db.close()
    with _lock:
        building, since, error = _building(), _state.building_since, _state.last_error
    if error is None and latest is not None and latest.status == "failed":
        error = latest.error
    return {
        "status": "building" if building else ("ready" if run is not None else ("failed" if error else "idle")),
        "enabled": get_settings().i13_snapshot_enabled,
        "version": run.version if run else None,
        "reference_date": run.reference_date if run else None,
        "built_at": run.built_at if run else None,
        "build_seconds": float(run.build_seconds) if run and run.build_seconds is not None else None,
        "fingerprint": run.fingerprint if run else None,
        "building_since": since if building else None,
        "rebuilding": building and run is not None,
        "last_error": error,
    }

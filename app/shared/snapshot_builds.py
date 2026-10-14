"""One snapshot build at a time, per process: I08 and I13 never build side by side.

Both snapshots are assembled from full reads of the largest raw tables (MSEG,
EKBE, RESB) through the normalise views. Built at the same time on the nonprod
Azure SQL -- serverless, at most one vCore -- each starved the other. On
28-Sep the I13 start-up build took 134 s beside two I08 builds against 45 s
alone, and the I08 build's MSEG dispatch query ran past its statement limit on
every attempt, so the Repairable Spares pages never loaded.

So every full build of either snapshot runs inside :func:`exclusive_build`,
whatever started it -- start-up, a retry after failure, a data-change rebuild,
a manual refresh. A build that finds the other one running waits for it and
says so in the log. Start-up order (I08 first, then I13) is set in
``app.main``; this lock is what keeps the later triggers from overlapping.

Across processes and App Service instances too. The in-process lock cannot see
another process, and the App Service runs on more than one instance at times
(and always starts a new container before it stops the old one), so a build
also holds a SQL Server application lock for its whole life
(``sp_getapplock``, session-owned -- the scheduler's ``_Lock``). A build on
another instance therefore *waits its turn* rather than starting beside this
one; every instance still builds its own in-memory snapshot, just not at the
same moment. The lock belongs to the database session, so a build process that
is killed releases it the moment its connection drops.

The database lock is a guard, never a gate. With no database, on anything but
SQL Server, or if the lock cannot be taken, the build goes ahead exactly as it
did before this lock existed; and a wait is bounded, so a hung build elsewhere
cannot keep this instance without a snapshot forever.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from app.core.logging import get_logger

logger = get_logger(__name__)

# Reentrant so a build that ever ends up calling into the other cannot deadlock
# itself. Neither does today.
_lock = threading.RLock()

#: The cross-instance lock. One name for both snapshots, like the in-process one:
#: they must not build side by side whichever instance each runs on.
BUILD_LOCK_NAME = "spares_ai_snapshot_build"

#: Seconds between attempts while another instance's build holds the lock. Short
#: next to a build (minutes), long enough not to hammer the database.
POLL_SECONDS = 15.0

#: The longest this build waits for another instance's before going ahead
#: without the lock. Generous -- an I08 build was 568 s on nonprod -- but finite.
MAX_WAIT_SECONDS = 1800.0

# Only the outermost ``exclusive_build`` of a thread touches the database lock:
# the in-process lock is reentrant, and taking the session lock twice from one
# thread would be a second connection waiting on the first.
_held = threading.local()


def _new_database_lock():
    """The cross-instance lock for this database, or ``None`` where there is no
    SQL Server to hold one (no database configured, or another backend)."""
    from app.core.db import DatabaseNotConfiguredError, get_engine

    try:
        dialect = get_engine().dialect.name
    except DatabaseNotConfiguredError:
        return None  # nothing to guard, and nothing worth a warning
    if dialect != "mssql":
        return None
    from app.ingest.scheduler import _Lock

    return _Lock(BUILD_LOCK_NAME)


def acquire_database_lock(
    name: str,
    *,
    new_lock: Callable[[], object | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
):
    """Take the cross-instance build lock, waiting for another instance's build.

    Returns the held lock, or ``None`` when the build should simply go ahead
    unguarded: no SQL Server, the database could not be reached, or the wait
    ran out. Never raises -- a guard that can stop a snapshot from building is
    worse than none.
    """
    try:
        lock = (new_lock or _new_database_lock)()
        if lock is None:
            return None
        deadline = monotonic() + MAX_WAIT_SECONDS
        announced = False
        while not lock.acquire():
            if monotonic() >= deadline:
                logger.warning(
                    "%s snapshot build: another instance's build still holds the build lock after "
                    "%.0f s; building anyway",
                    name, MAX_WAIT_SECONDS,
                )
                return None
            if not announced:
                logger.info("%s snapshot build waiting for a build on another instance to finish", name)
                announced = True
            sleep(POLL_SECONDS)
        return lock
    except Exception:  # noqa: BLE001 -- a missing guard must never stop a build
        logger.warning("%s snapshot build: could not use the cross-instance build lock; building without it", name, exc_info=True)
        return None


@contextmanager
def exclusive_build(name: str) -> Iterator[None]:
    """Hold the build slot for the ``name`` snapshot's build: the process-wide
    one, and -- on SQL Server -- the cross-instance one, in that order."""
    if not _lock.acquire(blocking=False):
        logger.info("%s snapshot build waiting for the build in progress to finish", name)
        _lock.acquire()
    outermost = getattr(_held, "depth", 0) == 0
    _held.depth = getattr(_held, "depth", 0) + 1
    database_lock = acquire_database_lock(name) if outermost else None
    try:
        yield
    finally:
        try:
            if database_lock is not None:
                database_lock.release()
        finally:
            _held.depth -= 1
            _lock.release()

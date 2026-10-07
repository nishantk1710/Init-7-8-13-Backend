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

Per process only. A second gunicorn worker is a second process with its own
lock and its own pair of builds, which is one more reason startup.sh runs one.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from app.core.logging import get_logger

logger = get_logger(__name__)

# Reentrant so a build that ever ends up calling into the other cannot deadlock
# itself. Neither does today.
_lock = threading.RLock()


@contextmanager
def exclusive_build(name: str) -> Iterator[None]:
    """Hold the process-wide build slot for the ``name`` snapshot's build."""
    if not _lock.acquire(blocking=False):
        logger.info("%s snapshot build waiting for the build in progress to finish", name)
        _lock.acquire()
    try:
        yield
    finally:
        _lock.release()

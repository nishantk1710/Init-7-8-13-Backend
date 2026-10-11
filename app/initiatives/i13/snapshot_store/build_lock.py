"""The one-build-at-a-time lock, held in SQL Server.

A build used to be guarded only by a flag in the API process. That flag cannot
see another process, and on 10-11 Oct 2026 two kinds of "other process" kept
destroying each other's builds:

* Azure restarts an App Service by starting the new container BEFORE stopping
  the old one, so two copies of the API run side by side for a while;
* start-up's clean-up of interrupted builds ran while a page request had just
  started a new build in the same process, and deleted its working tables.

So a build now holds a SQL Server application lock for its whole life
(``sp_getapplock``, session-owned -- the scheduler's ``_Lock``), and anything
that would touch build state from outside -- start-up clean-up, starting
another build -- first checks it. The lock belongs to the database session, so
a build process that is killed releases it the moment its connection drops:
"held" always means a build is really running somewhere.
"""

from __future__ import annotations

from sqlalchemy import text

from app.core.db import get_engine
from app.ingest.scheduler import _Lock

LOCK_NAME = "spares_ai_i13_snapshot_build"


class BuildLock(_Lock):
    def __init__(self) -> None:
        super().__init__(LOCK_NAME)


def held() -> bool:
    """Whether a build holds the lock right now, in any process."""
    with get_engine().connect() as connection:
        grantable = connection.execute(
            text("SELECT APPLOCK_TEST('public', :name, 'Exclusive', 'Session')"), {"name": LOCK_NAME}
        ).scalar()
    return grantable == 0

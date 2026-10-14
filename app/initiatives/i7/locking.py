"""A cross-instance lock for one I07 stage.

``sp_getapplock`` with a session owner, the same mechanism the pipeline
orchestrator and the ingest scheduler use (``app.ingest.scheduler._Lock``): held
for the life of one connection and released by SQL Server itself if the process
dies, so a killed run can never leave it held. Holding it proves no other run of
that stage is alive -- which is what makes it safe to judge a ``running`` row
dead, and to resume it.

Only on SQL Server. Anywhere else (the SQLite tests) :class:`NoLock` stands in:
every real deployment is Azure SQL.
"""

from __future__ import annotations

from typing import Any


class NoLock:
    """Always granted. For databases that have no application locks."""

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        return None


def application_lock(name: str) -> Any:
    """The named lock for this database: a real one on SQL Server, else :class:`NoLock`."""
    from app.core.db import get_engine

    if get_engine().dialect.name != "mssql":
        return NoLock()
    from app.ingest.scheduler import _Lock

    return _Lock(name)

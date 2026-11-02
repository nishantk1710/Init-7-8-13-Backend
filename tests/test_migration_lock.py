"""`alembic upgrade head` from two instances at once: the second waits its turn.

startup.sh runs the migrations on every instance when RUN_MIGRATIONS_ON_STARTUP
is on. alembic/env.py now holds a SQL Server application lock for the whole
upgrade, so an instance that starts while another is migrating waits, then
finds the revisions applied. Needs a SQL Server (CI's container), and runs
alembic in a process of its own: env.py configures logging on import, which
would otherwise rewire this test session's.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import MSSQL, backend_of

BACKEND = Path(__file__).resolve().parents[1]

# alembic/env.py's MIGRATION_LOCK_NAME. If the two drift apart the subprocess
# below does not wait, and this test fails -- which is the point.
MIGRATION_LOCK_NAME = "spares_ai_migrations"


def _sql_server_configured() -> bool:
    url = get_settings().database_url
    return bool(url) and backend_of(url) == MSSQL


def test_env_py_names_the_lock_this_test_holds() -> None:
    assert f'MIGRATION_LOCK_NAME = "{MIGRATION_LOCK_NAME}"' in (BACKEND / "alembic" / "env.py").read_text()


@pytest.mark.skipif(not _sql_server_configured(), reason="needs a SQL Server DATABASE_URL")
def test_a_second_migrator_waits_for_the_first_then_finishes() -> None:
    from app.core.db import get_engine
    from app.ingest.scheduler import _Lock

    def waiting_for_an_application_lock() -> int:
        with get_engine().connect() as c:
            return c.execute(text(
                "SELECT COUNT(*) FROM sys.dm_tran_locks "
                "WHERE resource_type = 'APPLICATION' AND request_status = 'WAIT'"
            )).scalar() or 0

    first = _Lock(MIGRATION_LOCK_NAME)
    assert first.acquire(), "another migration is running on this database"
    second = subprocess.Popen(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + 90
        while not waiting_for_an_application_lock():
            assert second.poll() is None, f"alembic did not wait for the lock:\n{second.communicate()[0]}"
            assert time.monotonic() < deadline, "alembic never reached the migration lock"
            time.sleep(0.5)
        assert second.poll() is None
    finally:
        first.release()

    try:
        output, _ = second.communicate(timeout=120)
    finally:
        if second.poll() is None:
            second.kill()
    assert second.returncode == 0, output

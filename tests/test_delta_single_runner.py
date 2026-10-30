"""One delta cycle at a time across every worker and instance.

Each gunicorn worker imports the scheduler and runs its own timer; the SQL
Server application lock in ``_tick`` is what makes exactly one of them run each
cycle. These pin that: a refused lock runs nothing, a held one is always let
go, and on a real SQL Server a second session is refused while the first holds.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import get_settings
from app.core.db import MSSQL, backend_of
from app.ingest import scheduler

SUMMARY = {"fetched": 0, "loaded": 0, "rows": 0, "failed": 0, "skipped": 0, "errors": []}


class FakeLock:
    def __init__(self, granted: bool = True, error: Exception | None = None) -> None:
        self.granted, self.error = granted, error
        self.released = 0

    def acquire(self) -> bool:
        if self.error is not None:
            raise self.error
        return self.granted

    def release(self) -> None:
        self.released += 1


@pytest.fixture
def tick(monkeypatch):
    def run(lock: FakeLock, cycle=lambda: SUMMARY):
        names: list[str] = []
        ran: list[bool] = []

        def new_lock(name: str = scheduler.LOCK_NAME) -> FakeLock:
            names.append(name)
            return lock

        def run_cycle():
            ran.append(True)
            return cycle()

        monkeypatch.setattr(scheduler, "_Lock", new_lock)
        monkeypatch.setattr(scheduler, "run_delta_cycle", run_cycle)
        result = asyncio.run(scheduler._tick("test"))
        return result, ran, names

    return run


def test_a_worker_that_does_not_get_the_lock_runs_nothing(tick) -> None:
    lock = FakeLock(granted=False)
    result, ran, names = tick(lock)
    assert result is False and ran == []
    assert names == ["spares_ai_delta_ingest"]
    assert lock.released == 0


def test_the_worker_holding_the_lock_runs_the_cycle_and_lets_go(tick) -> None:
    lock = FakeLock()
    result, ran, _ = tick(lock)
    assert result is True and ran == [True]
    assert lock.released == 1


def test_a_cycle_that_raises_still_lets_go_of_the_lock(tick) -> None:
    def broken():
        raise RuntimeError("SAP down")

    lock = FakeLock()
    result, ran, _ = tick(lock, broken)
    assert result is True and ran == [True]
    assert lock.released == 1


def test_an_unreachable_lock_skips_the_tick_rather_than_running_unguarded(tick) -> None:
    result, ran, _ = tick(FakeLock(error=OSError("database unreachable")))
    assert result is False and ran == []


def test_the_full_refresh_is_kept_to_one_runner_the_same_way(monkeypatch) -> None:
    ran: list[bool] = []
    names: list[str] = []

    def new_lock(name: str = scheduler.LOCK_NAME) -> FakeLock:
        names.append(name)
        return FakeLock(granted=False)

    monkeypatch.setattr(scheduler, "_Lock", new_lock)
    monkeypatch.setattr(scheduler, "run_full_refresh_cycle", lambda: ran.append(True))
    asyncio.run(scheduler._full_refresh_tick())
    assert ran == [] and names == ["spares_ai_full_refresh"]


def _sql_server_configured() -> bool:
    url = get_settings().database_url
    return bool(url) and backend_of(url) == MSSQL


@pytest.mark.skipif(not _sql_server_configured(), reason="needs a SQL Server DATABASE_URL")
def test_on_sql_server_a_second_worker_is_refused_while_the_first_holds_the_lock() -> None:
    first, second = scheduler._Lock(), scheduler._Lock()
    assert first.acquire()
    try:
        assert second.acquire() is False
    finally:
        first.release()
    assert second.acquire()
    second.release()

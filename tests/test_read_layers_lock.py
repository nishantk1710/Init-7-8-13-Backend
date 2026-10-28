"""One rebuild of the read layers (n_<table> and I08's views) at a time.

Start-up and every load rebuild them with CREATE OR ALTER VIEW; two at once --
two workers or instances starting together, a restart during the nightly
load -- deadlock on each other's schema locks. A SQL Server application lock
now serialises them: start-up skips when another process is rebuilding, a
load waits its turn, and nothing about the lock can stop a rebuild.

The lock, the sleeping and the clock are fakes here; the real lock is
exercised where a SQL Server is configured (CI's container).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.core.db import MSSQL, backend_of
from app.shared import sap_normalise
from app.shared.sap_normalise import (
    READ_LAYERS_LOCK_NAME,
    READ_LAYERS_MAX_WAIT_SECONDS,
    READ_LAYERS_POLL_SECONDS,
    _take_read_layers_lock,
)

SQL_SERVER = SimpleNamespace(dialect=SimpleNamespace(name="mssql"))
SQLITE = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))


class FakeLock:
    """Busy for the first ``busy_for`` acquire() calls, then granted."""

    def __init__(self, busy_for: int = 0, error: Exception | None = None) -> None:
        self.busy_for, self.error = busy_for, error
        self.attempts = 0
        self.released = 0

    def acquire(self) -> bool:
        self.attempts += 1
        if self.error is not None:
            raise self.error
        return self.attempts > self.busy_for

    def release(self) -> None:
        self.released += 1


class Clock:
    """A clock that only moves when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def take(lock, *, wait: bool, clock: Clock | None = None):
    clock = clock or Clock()
    return _take_read_layers_lock(
        SQL_SERVER, wait=wait, new_lock=lambda engine: lock, sleep=clock.sleep, monotonic=clock.monotonic
    )


# --- Taking the lock -------------------------------------------------------------


@pytest.mark.parametrize("wait", [True, False])
def test_a_free_lock_is_taken_at_once(wait: bool) -> None:
    lock, clock = FakeLock(), Clock()
    assert take(lock, wait=wait, clock=clock) == (True, lock)
    assert clock.sleeps == []


def test_start_up_leaves_a_rebuild_in_progress_to_whoever_holds_it(caplog) -> None:
    lock, clock = FakeLock(busy_for=99), Clock()
    with caplog.at_level(logging.INFO):
        assert take(lock, wait=False, clock=clock) == (False, None)
    assert lock.attempts == 1 and clock.sleeps == []
    assert "leaving it to that one" in caplog.text


def test_a_load_waits_its_turn_then_rebuilds_holding_the_lock() -> None:
    lock, clock = FakeLock(busy_for=3), Clock()
    assert take(lock, wait=True, clock=clock) == (True, lock)
    assert clock.sleeps == [READ_LAYERS_POLL_SECONDS] * 3


def test_a_wait_is_bounded_and_the_rebuild_goes_ahead_without_the_lock(caplog) -> None:
    lock, clock = FakeLock(busy_for=10**9), Clock()
    with caplog.at_level(logging.WARNING):
        assert take(lock, wait=True, clock=clock) == (True, None)
    assert clock.now >= READ_LAYERS_MAX_WAIT_SECONDS
    assert "rebuilding without it" in caplog.text


@pytest.mark.parametrize("wait", [True, False])
def test_a_lock_that_cannot_be_reached_never_stops_a_rebuild(wait: bool, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert take(FakeLock(error=OSError("unreachable")), wait=wait) == (True, None)
    assert "rebuilding without it" in caplog.text


def test_no_sql_server_means_no_lock() -> None:
    assert sap_normalise._read_layers_lock(SQLITE) is None
    assert _take_read_layers_lock(SQLITE, wait=True) == (True, None)


def test_on_sql_server_the_lock_is_the_schedulers_kind_under_its_own_name() -> None:
    from app.ingest.scheduler import _Lock

    lock = sap_normalise._read_layers_lock(SQL_SERVER)
    assert isinstance(lock, _Lock)
    assert lock._name == READ_LAYERS_LOCK_NAME == "spares_ai_read_layers"


# --- The rebuild under it --------------------------------------------------------


@pytest.fixture
def rebuild(monkeypatch):
    """rebuild_read_layers on a SQL Server engine with the views and the lock faked."""
    events: list[str] = []
    state = SimpleNamespace(lock=FakeLock(), fail=None, events=events)

    def ensure_views(engine):
        events.append("normalise views")
        if state.fail is not None:
            raise state.fail

    monkeypatch.setattr(sap_normalise, "ensure_views", ensure_views)
    monkeypatch.setattr("app.initiatives.i8.views.ensure_views", lambda engine: events.append("i8 views"))

    class Recording(FakeLock):
        def acquire(self) -> bool:
            granted = state.lock.acquire()
            events.append("lock taken" if granted else "lock busy")
            return granted

        def release(self) -> None:
            state.lock.release()
            events.append("lock released")

    monkeypatch.setattr(sap_normalise, "_read_layers_lock", lambda engine: Recording())
    monkeypatch.setattr(sap_normalise, "READ_LAYERS_POLL_SECONDS", 0.0)
    state.run = lambda **kwargs: sap_normalise.rebuild_read_layers(SQL_SERVER, **kwargs)
    return state


def test_the_views_are_rebuilt_inside_the_lock(rebuild) -> None:
    assert rebuild.run() is True
    assert rebuild.events == ["lock taken", "normalise views", "i8 views", "lock released"]


def test_start_up_skips_and_reports_success_while_another_rebuild_runs(rebuild) -> None:
    rebuild.lock = FakeLock(busy_for=99)
    assert rebuild.run(at_start_up=True) is True
    assert rebuild.events == ["lock busy"]
    assert rebuild.lock.released == 0, "never held, never released"


def test_after_a_load_the_rebuild_waits_and_then_runs(rebuild) -> None:
    rebuild.lock = FakeLock(busy_for=2)
    assert rebuild.run() is True
    assert rebuild.events == ["lock busy", "lock busy", "lock taken", "normalise views", "i8 views", "lock released"]


def test_a_failed_rebuild_still_releases_the_lock(rebuild) -> None:
    rebuild.fail = RuntimeError("view broke")
    assert rebuild.run() is False
    assert rebuild.events[-1] == "lock released"


def test_the_cli_still_sees_the_error_and_the_lock_is_still_released(rebuild) -> None:
    rebuild.fail = RuntimeError("view broke")
    with pytest.raises(RuntimeError, match="view broke"):
        rebuild.run(raise_errors=True)
    assert rebuild.events[-1] == "lock released"


def test_a_load_still_calls_the_rebuild_the_way_it_always_did(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(sap_normalise, "rebuild_read_layers", lambda **kwargs: calls.append(kwargs) or True)
    assert sap_normalise.refresh_after_load("mseg") is True
    assert calls == [{}], "waits its turn: no at_start_up"


# --- The real lock -------------------------------------------------------------------


def _sql_server_configured() -> bool:
    url = get_settings().database_url
    return bool(url) and backend_of(url) == MSSQL


@pytest.mark.skipif(not _sql_server_configured(), reason="needs a SQL Server DATABASE_URL")
def test_on_sql_server_a_second_process_is_refused_until_the_first_lets_go() -> None:
    from app.core.db import get_engine

    engine = get_engine()
    held, other = sap_normalise._read_layers_lock(engine), None
    assert held.acquire()
    try:
        assert _take_read_layers_lock(engine, wait=False) == (False, None)
    finally:
        held.release()
    rebuild, other = _take_read_layers_lock(engine, wait=False)
    try:
        assert rebuild is True and other is not None
    finally:
        if other is not None:
            other.release()

"""The cross-instance build lock: one snapshot build at a time, on every instance.

``exclusive_build`` already kept I08 and I13 from building side by side inside
one process. The App Service runs on more than one instance at times (and starts
the new container before stopping the old one), so each instance used to build
at once and starved the one-vCore database. A build now also holds a SQL Server
application lock, and a build elsewhere waits its turn.

No database: the lock, the sleeping and the clock are all fakes.
"""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

import pytest

from app.shared import snapshot_builds
from app.shared.snapshot_builds import acquire_database_lock, exclusive_build


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


def acquire(lock, clock=None, name="I08"):
    clock = clock or Clock()
    return acquire_database_lock(
        name, new_lock=lambda: lock, sleep=clock.sleep, monotonic=clock.monotonic
    ), clock


def test_a_free_lock_is_taken_at_once_without_waiting() -> None:
    lock = FakeLock()
    held, clock = acquire(lock)
    assert held is lock and clock.sleeps == [] and lock.attempts == 1


def test_a_build_on_another_instance_is_waited_for_then_the_lock_is_taken(caplog) -> None:
    lock = FakeLock(busy_for=3)
    with caplog.at_level(logging.INFO, logger=snapshot_builds.logger.name):
        held, clock = acquire(lock)
    assert held is lock and lock.attempts == 4
    assert clock.sleeps == [snapshot_builds.POLL_SECONDS] * 3
    waiting = [r for r in caplog.records if "waiting for a build on another instance" in r.getMessage()]
    assert len(waiting) == 1  # said once, not on every poll


def test_a_wait_is_bounded_and_the_build_goes_ahead_without_the_lock(caplog) -> None:
    lock = FakeLock(busy_for=10**9)  # another instance's build never finishes
    with caplog.at_level(logging.WARNING, logger=snapshot_builds.logger.name):
        held, clock = acquire(lock)
    assert held is None
    assert snapshot_builds.MAX_WAIT_SECONDS <= clock.now < snapshot_builds.MAX_WAIT_SECONDS + snapshot_builds.POLL_SECONDS
    assert any("building anyway" in r.getMessage() for r in caplog.records)
    assert lock.released == 0  # never held, so never released


def test_no_sql_server_means_no_lock_and_no_wait() -> None:
    held, clock = acquire_database_lock("I08", new_lock=lambda: None), Clock()
    assert held is None and clock.sleeps == []


def test_a_database_error_never_stops_a_build(caplog) -> None:
    lock = FakeLock(error=ConnectionError("database unreachable"))
    with caplog.at_level(logging.WARNING, logger=snapshot_builds.logger.name):
        held, _ = acquire(lock)
    assert held is None
    assert any("building without it" in r.getMessage() for r in caplog.records)


# The two tests below exercise the REAL lock factory. They must not depend on the
# environment: on a laptop there is no database, but CI has a real SQL Server, and
# a factory call there takes a real application lock that nothing here releases --
# which made every later build in the process wait out the 30-minute bound.


def test_with_no_database_configured_nothing_is_guarded(monkeypatch) -> None:
    from app.core import db

    def refuse():
        raise db.DatabaseNotConfiguredError("no database")

    monkeypatch.setattr(db, "get_engine", refuse)
    assert snapshot_builds._new_database_lock() is None
    assert acquire_database_lock("I08") is None  # quiet: nothing to guard, nothing to warn about


def test_a_database_that_is_not_sql_server_is_not_guarded(monkeypatch) -> None:
    from app.core import db

    monkeypatch.setattr(db, "get_engine", lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")))
    assert snapshot_builds._new_database_lock() is None


# --- through exclusive_build ----------------------------------------------


@pytest.fixture
def fake_lock(monkeypatch):
    lock = FakeLock()
    monkeypatch.setattr(snapshot_builds, "_new_database_lock", lambda: lock)
    return lock


def test_a_build_holds_the_database_lock_for_its_whole_life_and_releases_it(fake_lock) -> None:
    with exclusive_build("I08"):
        assert fake_lock.attempts == 1 and fake_lock.released == 0
    assert fake_lock.released == 1


def test_the_lock_is_released_when_the_build_fails(fake_lock) -> None:
    with pytest.raises(RuntimeError):
        with exclusive_build("I08"):
            raise RuntimeError("the build failed")
    assert fake_lock.released == 1
    with exclusive_build("I13"):  # and the next build is not blocked by it
        pass
    assert fake_lock.released == 2


def test_a_nested_build_in_the_same_thread_takes_the_database_lock_once(fake_lock) -> None:
    with exclusive_build("I08"):
        with exclusive_build("I13"):
            assert fake_lock.attempts == 1
    assert fake_lock.released == 1


def test_builds_in_one_process_still_queue_and_hold_the_lock_one_at_a_time(monkeypatch) -> None:
    """The in-process behaviour is unchanged: the second build waits for the
    first, and only one of them ever holds the database lock."""
    state = {"holders": 0, "max_holders": 0}

    class Counting(FakeLock):
        def acquire(self) -> bool:
            state["holders"] += 1
            state["max_holders"] = max(state["max_holders"], state["holders"])
            return True

        def release(self) -> None:
            state["holders"] -= 1

    monkeypatch.setattr(snapshot_builds, "_new_database_lock", lambda: Counting())
    order: list[str] = []
    first_in, let_go = threading.Event(), threading.Event()

    def first() -> None:
        with exclusive_build("I08"):
            order.append("I08 start")
            first_in.set()
            let_go.wait(5)
            order.append("I08 end")

    def second() -> None:
        with exclusive_build("I13"):
            order.append("I13 start")

    a = threading.Thread(target=first)
    a.start()
    assert first_in.wait(5)
    b = threading.Thread(target=second)
    b.start()
    assert order == ["I08 start"]  # the second is waiting
    let_go.set()
    a.join(5)
    b.join(5)
    assert order == ["I08 start", "I08 end", "I13 start"]
    assert state["max_holders"] == 1 and state["holders"] == 0


def test_a_build_with_no_lock_available_runs_exactly_as_before(monkeypatch) -> None:
    monkeypatch.setattr(snapshot_builds, "_new_database_lock", lambda: None)
    ran = []
    with exclusive_build("I08"):
        ran.append(True)
    assert ran == [True]


def test_both_snapshots_share_one_lock_name() -> None:
    """They must not build side by side on different instances either."""
    assert snapshot_builds.BUILD_LOCK_NAME == "spares_ai_snapshot_build"

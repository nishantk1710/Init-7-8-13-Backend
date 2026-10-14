"""The nightly I07 trigger: when it asks, when it runs, and when it must not.

Offline: an in-memory SQLite database holding only ``i7_pipeline_run`` and
``ingestion_run``, a fixed clock, and a fake runner standing in for
``watch.check_and_run``. The real change check's SQL runs (it is what decides),
but the real pipeline is never started -- nothing here can launch a run.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.initiatives.i7 import pipeline_scheduler as scheduler
from app.initiatives.i7.pipeline_scheduler import (
    InvalidScheduleError,
    SchedulePolicy,
    open_slot,
    tick,
)
from app.models.base import Base
from app.models.i7_pipeline import STATUS_ABANDONED, STATUS_FAILED, STATUS_RUNNING, STATUS_SUCCEEDED, PipelineRun
from app.models.ingestion import IngestionRun

UTC = timezone.utc
DAY = datetime(2026, 10, 13, tzinfo=UTC)


def at(hour: int, minute: int = 0, *, day: int = 0) -> datetime:
    return DAY + timedelta(days=day, hours=hour, minutes=minute)


POLICY = SchedulePolicy(hour_utc=20, retries=2, interval_seconds=300, initial_delay_seconds=0)


def settings(**overrides):
    base = dict(
        i7_pipeline_watch_enabled=True,
        i7_pipeline_nightly_hour_utc=20,
        i7_pipeline_retries=2,
        i7_pipeline_check_interval_seconds=300,
        i7_pipeline_initial_delay_seconds=120,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[PipelineRun.__table__, IngestionRun.__table__])
    return sessionmaker(bind=engine)


def load(factory, table="raw_marc") -> None:
    """A raw-layer load: moves the change fingerprint."""
    with factory() as session:
        session.add(
            IngestionRun(
                source_file="MARC.csv", target_table=table, row_count=1, status="succeeded",
                started_at=at(1), finished_at=at(1),
            )
        )
        session.commit()


def fingerprint(factory) -> str:
    from app.initiatives.i7.watch import source_fingerprint

    with factory() as session:
        return source_fingerprint(session)


def add_run(factory, status, when, *, fp=None, reason="nightly") -> None:
    with factory() as session:
        session.add(
            PipelineRun(
                status=status, trigger_reason=reason, source_fingerprint=fp or fingerprint(factory),
                started_at=when,
            )
        )
        session.commit()


class Runner:
    """Stands in for ``watch.check_and_run``. Records a pipeline run row the way
    the orchestrator would, so later ticks see it."""

    def __init__(self, factory, status=STATUS_SUCCEEDED, when=None) -> None:
        self.factory, self.status, self.when = factory, status, when
        self.calls: list[str] = []

    def __call__(self, reason: str) -> str:
        self.calls.append(reason)
        add_run(self.factory, self.status, self.when or at(20, 1), reason=reason)
        return "ran"


def go(factory, runner, now) -> str:
    return tick(POLICY, session_factory=factory, runner=runner, now=now)


# --- settings -------------------------------------------------------------


def test_disabled_starts_nothing(factory) -> None:
    stop = threading.Event()
    assert scheduler.start(stop, settings(i7_pipeline_watch_enabled=False)) is None


def test_the_default_is_off() -> None:
    from app.core.config import Settings

    s = Settings(_env_file=None)
    assert (s.i7_pipeline_watch_enabled, s.i7_pipeline_nightly_hour_utc, s.i7_pipeline_retries) == (False, 20, 2)


def test_valid_settings_become_a_policy() -> None:
    assert SchedulePolicy.from_settings(settings()) == SchedulePolicy(20, 2, 300, 120)


@pytest.mark.parametrize(
    "bad",
    [
        {"i7_pipeline_nightly_hour_utc": 24},
        {"i7_pipeline_nightly_hour_utc": -1},
        {"i7_pipeline_retries": -1},
        {"i7_pipeline_retries": 9},
        {"i7_pipeline_check_interval_seconds": 1},
        {"i7_pipeline_check_interval_seconds": 7200},
        {"i7_pipeline_initial_delay_seconds": -5},
    ],
)
def test_invalid_settings_are_refused(bad) -> None:
    with pytest.raises(InvalidScheduleError):
        SchedulePolicy.from_settings(settings(**bad))


def test_invalid_settings_leave_the_timer_off_without_raising() -> None:
    assert scheduler.start(threading.Event(), settings(i7_pipeline_retries=99)) is None


# --- slots ----------------------------------------------------------------


def test_slots_open_at_the_hour_and_every_two_hours_for_an_hour() -> None:
    assert open_slot(at(19, 59), POLICY) is None
    assert open_slot(at(20, 0), POLICY) == at(20)
    assert open_slot(at(20, 59), POLICY) == at(20)
    assert open_slot(at(21, 0), POLICY) is None
    assert open_slot(at(22, 30), POLICY) == at(22)
    assert open_slot(at(12), POLICY) is None


def test_the_last_retry_slot_runs_past_midnight() -> None:
    assert open_slot(at(0, 10, day=1), POLICY) == at(24)  # yesterday's third slot


def test_no_retries_means_one_slot() -> None:
    one = SchedulePolicy(20, 0, 300, 0)
    assert open_slot(at(20, 5), one) == at(20)
    assert open_slot(at(22, 5), one) is None


def test_a_naive_time_is_refused() -> None:
    with pytest.raises(ValueError):
        open_slot(datetime(2026, 10, 13, 20), POLICY)


# --- the decision ---------------------------------------------------------


def test_outside_a_slot_nothing_is_read_or_run(factory) -> None:
    load(factory)
    runner = Runner(factory)
    assert go(factory, runner, at(14)) == "no slot open"
    assert runner.calls == []


def test_unchanged_data_is_skipped(factory) -> None:
    load(factory)
    add_run(factory, STATUS_SUCCEEDED, at(2))  # a succeeded run covering today's fingerprint
    runner = Runner(factory)
    assert "unchanged since the last successful run" in go(factory, runner, at(20, 5))
    assert runner.calls == []


def test_changed_data_runs_the_orchestrator_once_as_nightly(factory) -> None:
    load(factory)
    add_run(factory, STATUS_SUCCEEDED, at(2), fp="old-fingerprint")
    runner = Runner(factory)
    assert go(factory, runner, at(20, 5)) == "ran"
    assert runner.calls == ["nightly"]


def test_no_data_at_all_runs_nothing(factory) -> None:
    runner = Runner(factory)
    assert "no successful load" in go(factory, runner, at(20, 5))
    assert runner.calls == []


def test_first_night_with_no_recorded_run_runs_once(factory) -> None:
    """The documented first-run behaviour: manual SSH runs leave no pipeline
    row, so the first enabled night runs the whole pipeline once."""
    load(factory)
    runner = Runner(factory)
    assert go(factory, runner, at(20, 5)) == "ran"
    assert go(factory, runner, at(20, 10)).startswith("a run already started")  # and only once
    assert len(runner.calls) == 1


def test_a_succeeded_run_is_never_repeated_by_later_ticks_or_slots(factory) -> None:
    load(factory)
    runner = Runner(factory, STATUS_SUCCEEDED)
    go(factory, runner, at(20, 5))
    for later in (at(20, 10), at(20, 55), at(22, 5), at(0, 5, day=1)):
        go(factory, runner, later)
    assert len(runner.calls) == 1


def test_many_ticks_in_one_slot_start_one_run(factory) -> None:
    load(factory)
    runner = Runner(factory, STATUS_RUNNING)
    results = [go(factory, runner, at(20, m)) for m in range(5, 55, 5)]
    assert len(runner.calls) == 1
    assert all(r.startswith("a run already started") for r in results[1:])


def test_a_failed_run_is_retried_in_the_next_slot(factory) -> None:
    load(factory)
    runner = Runner(factory, STATUS_FAILED, at(20, 6))
    go(factory, runner, at(20, 5))
    assert go(factory, runner, at(21, 30)) == "no slot open"
    runner.when = at(22, 6)
    assert go(factory, runner, at(22, 5)) == "ran"
    assert len(runner.calls) == 2


def test_an_interrupted_run_counts_and_is_retried(factory) -> None:
    """A killed run is left 'running' until the next lock-holder abandons it."""
    load(factory)
    add_run(factory, STATUS_RUNNING, at(20, 6))  # the process died; the row says running
    runner = Runner(factory, STATUS_FAILED, at(22, 6))
    assert go(factory, runner, at(22, 5)) == "ran"


def test_the_retry_limit_stops_further_attempts_for_that_data(factory) -> None:
    load(factory)
    add_run(factory, STATUS_FAILED, at(20, 6))
    add_run(factory, STATUS_ABANDONED, at(22, 6))
    add_run(factory, STATUS_FAILED, at(0, 6, day=1))  # 3 attempts = 1 + 2 retries
    runner = Runner(factory)
    out = go(factory, runner, at(0, 20, day=1))
    assert out.startswith("a run already started") or "retries exhausted" in out
    # Next evening, same data: still exhausted -- the slot itself is clean.
    assert "retries exhausted (3/3" in go(factory, runner, at(20, 5, day=1))
    assert runner.calls == []


def test_new_data_resets_the_count(factory) -> None:
    load(factory)
    for hour in (20, 22):
        add_run(factory, STATUS_FAILED, at(hour, 6))
    add_run(factory, STATUS_FAILED, at(0, 6, day=1))
    load(factory, "raw_mseg")  # new data: a new fingerprint
    runner = Runner(factory)
    assert go(factory, runner, at(20, 5, day=1)) == "ran"


def test_zero_retries_tries_each_data_version_once(factory) -> None:
    load(factory)
    add_run(factory, STATUS_FAILED, at(20, 6))
    runner = Runner(factory)
    out = tick(SchedulePolicy(20, 0, 300, 0), session_factory=factory, runner=runner, now=at(20, 30))
    assert out.startswith("a run already started")
    out = tick(SchedulePolicy(20, 0, 300, 0), session_factory=factory, runner=runner, now=at(20, 5, day=1))
    assert "retries exhausted (1/1" in out


def test_state_survives_a_restart_because_it_lives_in_the_database(factory, monkeypatch) -> None:
    load(factory)
    add_run(factory, STATUS_FAILED, at(20, 6))
    add_run(factory, STATUS_FAILED, at(22, 6))
    # "Restart": nothing from the earlier process exists -- a new factory over the same engine.
    reborn = sessionmaker(bind=factory.kw["bind"])
    runner = Runner(reborn, STATUS_FAILED, at(0, 6, day=1))
    assert go(reborn, runner, at(0, 5, day=1)) == "ran"  # the third and last attempt
    assert "retries exhausted" in go(reborn, runner, at(20, 5, day=1))


def test_two_instances_ticking_together_start_one_run(factory) -> None:
    load(factory)
    a, b = Runner(factory, STATUS_RUNNING), Runner(factory, STATUS_RUNNING)
    go(factory, a, at(20, 5))
    go(factory, b, at(20, 5))
    assert len(a.calls) + len(b.calls) == 1


def test_a_busy_lock_is_passed_through_not_treated_as_a_failure(factory) -> None:
    """A manual run holds the lock: the orchestrator says so and the tick ends quietly."""
    load(factory)
    out = go(factory, lambda reason: "skipped: another I07 pipeline run holds the lock", at(20, 5))
    assert out.startswith("skipped")


def test_a_manual_run_started_in_the_slot_is_not_overlapped(factory) -> None:
    load(factory)
    add_run(factory, STATUS_RUNNING, at(20, 2), reason="manual")
    runner = Runner(factory)
    assert go(factory, runner, at(20, 5)).startswith("a run already started")
    assert runner.calls == []


def test_a_database_failure_in_one_tick_does_not_raise(factory) -> None:
    def broken():
        raise RuntimeError("connection reset")

    runner = Runner(factory)
    out = tick(POLICY, session_factory=broken, runner=runner, now=at(20, 5))
    assert out.startswith("check failed: RuntimeError")
    assert runner.calls == []


# --- the loop -------------------------------------------------------------


def test_the_loop_stops_at_once_when_asked(factory) -> None:
    stop = threading.Event()
    stop.set()
    runner = Runner(factory)
    scheduler.run_loop(stop, POLICY, session_factory=factory, runner=runner)  # returns, does not hang
    assert runner.calls == []


def test_the_loop_survives_a_failing_tick_and_stops(factory) -> None:
    load(factory)
    stop = threading.Event()
    ticks: list[int] = []

    def runner(reason: str) -> str:
        ticks.append(1)
        stop.set()
        raise RuntimeError("boom")  # the loop must absorb it

    quick = SchedulePolicy(20, 2, 5, 0)
    scheduler.run_loop(stop, quick, session_factory=factory, runner=runner, clock=lambda: at(20, 5))
    assert ticks == [1]


def test_start_creates_a_daemon_thread_only_when_enabled(factory, monkeypatch) -> None:
    monkeypatch.setattr("app.core.db.get_sessionmaker", lambda: factory)
    stop = threading.Event()
    stop.set()  # the thread exits immediately
    thread = scheduler.start(stop, settings(i7_pipeline_initial_delay_seconds=0))
    assert thread is not None and thread.daemon and thread.name == "i7-pipeline-nightly"
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_start_up_never_runs_a_pipeline_by_itself(factory, monkeypatch) -> None:
    """Enabling the timer outside any slot must not run anything."""
    load(factory)
    runner = Runner(factory)
    monkeypatch.setattr("app.core.db.get_sessionmaker", lambda: factory)
    stop = threading.Event()
    quick = SchedulePolicy(20, 2, 5, 0)
    t = threading.Thread(
        target=scheduler.run_loop, args=(stop, quick),
        kwargs=dict(session_factory=factory, runner=runner, clock=lambda: at(9)), daemon=True,
    )
    t.start()
    stop.set()
    t.join(timeout=5)
    assert runner.calls == []


# --- application wiring ---------------------------------------------------


def test_the_application_only_starts_the_timer_when_the_flag_is_on() -> None:
    import inspect

    import app.main

    source = inspect.getsource(app.main)
    assert "pipeline_scheduler.start(stop, settings)" in source
    gate = source.index("i7_pipeline_watch_enabled")
    assert gate < source.index("pipeline_scheduler.start")
    # main still never names the check or the orchestrator itself.
    assert "check_and_run" not in source and "run_pipeline" not in source


def test_runs_are_recorded_with_the_nightly_reason(factory) -> None:
    load(factory)
    runner = Runner(factory)
    go(factory, runner, at(20, 5))
    with factory() as session:
        assert session.scalars(select(PipelineRun.trigger_reason)).all() == ["nightly"]

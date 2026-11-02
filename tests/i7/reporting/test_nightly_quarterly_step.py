"""The quarterly report as a step of the nightly I07 watcher -- the only in-app "automatic" path.

Offline: the real ``tick`` and the real step, an injected clock, in-memory SQLite, a fake pipeline runner and a fake lock.
Nights are *simulated by passing ``now``*, so the whole quarter boundary -- the last night of the old quarter, the first
night of the new one, and the one after -- runs in seconds with no waiting and no change to the system clock.
"""

from __future__ import annotations

import threading
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.initiatives.i7 import pipeline_scheduler as scheduler
from app.initiatives.i7.pipeline_scheduler import InvalidScheduleError, SchedulePolicy, quarterly_report_step, tick
from app.initiatives.i7.reporting.auto_generate import AutoResult, FAILED, GENERATED, generate_due_quarterly_report
from app.models.i7_reporting import QuarterlyReportRecord
from tests.i7.reporting.quarterly_fixtures import UTC, NoLock, make_factory, seed_q4_2026, stub_stockout

ON = SchedulePolicy(20, 2, 300, 0, quarterly_report_enabled=True)  # slots 20:00, 22:00, 00:00 UTC, one hour each
OFF = SchedulePolicy(20, 2, 300, 0)


def night(month, day, hour=20, minute=5) -> datetime:
    return datetime(2026 if month > 6 else 2027, month, day, hour, minute, tzinfo=UTC)


def settings(**overrides):
    base = dict(
        i7_pipeline_watch_enabled=True, i7_pipeline_nightly_hour_utc=20, i7_pipeline_retries=2,
        i7_pipeline_check_interval_seconds=300, i7_pipeline_initial_delay_seconds=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _stockout(monkeypatch):
    stub_stockout(monkeypatch)


@pytest.fixture
def factory():
    f = make_factory()
    with f() as session:
        seed_q4_2026(session)
    return f


def runner(reason: str) -> str:
    return "ran"


def locked_generate(session_factory, now, *, window_days):
    """The real generation, with the fake lock a SQLite test needs in place of SQL Server's."""
    return generate_due_quarterly_report(session_factory, now, window_days=window_days, lock=NoLock())


def saved(factory) -> list[QuarterlyReportRecord]:
    with factory() as session:
        return list(session.execute(select(QuarterlyReportRecord)).scalars())


# --- it is off unless asked for ---------------------------------------------------------------


def test_the_settings_default_to_off_and_a_week():
    from app.core.config import Settings

    s = Settings(_env_file=None)
    assert (s.i7_quarterly_report_auto_enabled, s.i7_quarterly_report_auto_window_days) == (False, 7)


def test_the_policy_reads_the_settings_and_defaults_off():
    assert SchedulePolicy.from_settings(settings()).quarterly_report_enabled is False
    on = SchedulePolicy.from_settings(settings(i7_quarterly_report_auto_enabled=True, i7_quarterly_report_auto_window_days=10))
    assert (on.quarterly_report_enabled, on.quarterly_report_window_days) == (True, 10)


@pytest.mark.parametrize("window", [0, -1, 32])
def test_a_bad_window_is_refused_only_when_the_step_is_on(window):
    with pytest.raises(InvalidScheduleError, match="WINDOW_DAYS"):
        SchedulePolicy.from_settings(settings(i7_quarterly_report_auto_enabled=True, i7_quarterly_report_auto_window_days=window))
    # Off: a setting for a feature that is off must not stop the pipeline watcher.
    assert SchedulePolicy.from_settings(settings(i7_quarterly_report_auto_window_days=window)).quarterly_report_enabled is False


def test_the_report_step_is_only_handed_to_the_thread_when_enabled(factory, monkeypatch):
    captured = {}

    class FakeThread:
        def __init__(self, target, args, kwargs, name, daemon):
            captured.update(kwargs)

        def start(self):
            pass

    monkeypatch.setattr("app.core.db.get_sessionmaker", lambda: factory)
    monkeypatch.setattr(scheduler.threading, "Thread", FakeThread)

    scheduler.start(threading.Event(), settings())
    assert captured["report"] is None
    scheduler.start(threading.Event(), settings(i7_quarterly_report_auto_enabled=True))
    assert callable(captured["report"])


def test_the_application_has_no_other_scheduler_for_the_report():
    """The step lives on the existing nightly timer; the app still starts no second one."""
    import inspect

    import app.main

    assert "generate_due_quarterly_report" not in inspect.getsource(app.main)
    assert "quarterly" not in inspect.getsource(app.main).lower()


# --- tick -------------------------------------------------------------------------------------


def test_the_step_runs_in_an_open_slot_after_the_pipeline_check_and_the_snapshots(factory):
    order = []
    out = tick(
        SchedulePolicy(20, 2, 300, 0, snapshots_enabled=True, quarterly_report_enabled=True),
        session_factory=factory, runner=runner, now=night(1, 1),
        capture=lambda sf, now: order.append("snapshots") or "captured",
        report=lambda sf, now: order.append("report") or "done",
    )
    assert order == ["snapshots", "report"]  # a quarter's last month is final before the report reads it
    assert out.endswith("snapshots: captured; quarterly report: done")


def test_the_step_does_not_run_outside_a_slot_or_when_the_policy_is_off(factory):
    calls = []
    report = lambda sf, now: calls.append(1) or "x"  # noqa: E731
    assert tick(ON, session_factory=factory, runner=runner, now=night(1, 1, hour=12), report=report) == "no slot open"
    tick(OFF, session_factory=factory, runner=runner, now=night(1, 1), report=report)
    tick(ON, session_factory=factory, runner=runner, now=night(1, 1))  # no step given
    assert calls == []


def test_a_failing_step_never_ends_the_tick_or_hides_the_pipeline_outcome(factory):
    def report(sf, now):
        raise RuntimeError("database blip")

    out = tick(ON, session_factory=factory, runner=runner, now=night(1, 1), report=report)
    assert out.startswith("no run:") or "ran" in out  # the pipeline half still reported
    assert "quarterly report failed: RuntimeError" in out


# --- the whole quarter boundary, night by night, with no waiting ------------------------------


def test_across_the_quarter_boundary_nothing_runs_until_the_quarter_closes_then_once(factory):
    step = quarterly_report_step(ON, generate=locked_generate)

    def go(now):
        return tick(ON, session_factory=factory, runner=runner, now=now, report=step)

    # Today, 27 Oct: Q3 closed weeks ago -- switching the step on generates nothing.
    assert "Q3 2026: OUTSIDE_WINDOW" in go(night(10, 27))
    # The last night of the quarter: Q4 is still running -- nothing is generated.
    assert "Q3 2026: OUTSIDE_WINDOW" in go(night(12, 31))
    assert saved(factory) == []

    # The first night of the new quarter: Q4 2026 is generated.
    assert "Q4 2026: GENERATED" in go(night(1, 1))
    [row] = saved(factory)
    assert (row.quarter, row.status) == ("Q4 2026", "COMPLETED")

    # Later ticks, in the same slot and on later nights, change nothing.
    assert "Q4 2026: UP_TO_DATE" in go(night(1, 1, minute=10))
    assert "Q4 2026: UP_TO_DATE" in go(night(1, 2))
    assert [(r.id, r.generated_at) for r in saved(factory)] == [(row.id, row.generated_at)]

    # Past the window: still nothing to do (and nothing missing, either).
    assert "Q4 2026: OUTSIDE_WINDOW" in go(night(1, 20))


# --- retries ----------------------------------------------------------------------------------


def test_a_failed_generation_is_retried_once_per_slot_not_once_per_tick(factory):
    calls = []

    def flaky(session_factory, now, *, window_days):
        calls.append(now)
        if len(calls) < 3:
            return AutoResult("Q4 2026", FAILED, "RuntimeError: boom")
        return locked_generate(session_factory, now, window_days=window_days)

    step = quarterly_report_step(ON, generate=flaky)
    go = lambda now: tick(ON, session_factory=factory, runner=runner, now=now, report=step)  # noqa: E731

    assert "Q4 2026: FAILED" in go(night(1, 1, 20, 5))  # slot 20:00, attempt 1 fails
    assert "FAILED earlier in the 20:00 slot" in go(night(1, 1, 20, 10))  # same slot: not asked again
    assert "FAILED earlier in the 20:00 slot" in go(night(1, 1, 20, 55))
    assert len(calls) == 1

    assert "Q4 2026: FAILED" in go(night(1, 1, 22, 5))  # the next slot: attempt 2, fails
    assert len(calls) == 2
    assert saved(factory) == []  # a failed attempt wrote nothing

    assert "Q4 2026: GENERATED" in go(night(1, 2, 0, 5))  # the slot after: attempt 3 succeeds
    assert len(calls) == 3 and len(saved(factory)) == 1


def test_a_retry_that_cannot_happen_in_a_slot_is_picked_up_the_next_night(factory):
    calls = []

    def always_failing(session_factory, now, *, window_days):
        calls.append(now)
        return AutoResult("Q4 2026", FAILED, "boom")

    step = quarterly_report_step(ON, generate=always_failing)
    for slot_hour in (20, 22, 0):  # every slot of the night is used once, no more
        for minute in (5, 20, 40):
            day = 1 if slot_hour else 2
            tick(ON, session_factory=factory, runner=runner, now=night(1, day, slot_hour, minute), report=step)
    assert len(calls) == 3

    # The next night is a fresh set of slots: it tries again, so a transient outage is not lost.
    tick(ON, session_factory=factory, runner=runner, now=night(1, 2, 20, 5), report=step)
    assert len(calls) == 4

"""A PR event starts the delta cycle: when, how often, and when it does not.

The cycle itself is stubbed (``_tick``); what is tested is the trigger -- the
switch, the debounce that lets a burst share one cycle, the extra cycle for an
event that lands mid-run, and the retry when another worker holds the lock.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.ingest import scheduler
from app.main import app


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(scheduler, "_event_task", None)
    monkeypatch.setattr(scheduler, "_event_again", False)
    monkeypatch.setattr(scheduler, "EVENT_RETRY_SECONDS", 0)


def _settings(monkeypatch, **overrides) -> Settings:
    base = dict(database_url="mssql+pyodbc://x", storage_url="abfss://y", cpi_base_url="", cpi_token_url="",
                cpi_client_id="", cpi_client_secret="", delta_on_pr_event_enabled=True,
                delta_event_debounce_seconds=0)
    base.update(overrides)
    s = Settings(_env_file=None, **base)
    monkeypatch.setattr(scheduler, "get_settings", lambda: s)
    return s


def _stub_tick(monkeypatch, results=None, during=None):
    """Replace the cycle. ``results`` -- what each call returns (True = it ran);
    ``during`` -- run inside a call, to model an event arriving mid-cycle."""
    calls: list[str] = []
    answers = list(results or [])

    async def tick(trigger: str = "timer") -> bool:
        calls.append(trigger)
        if during and len(calls) == 1:
            during()
        return answers.pop(0) if answers else True

    monkeypatch.setattr(scheduler, "_tick", tick)
    return calls


async def _drain() -> None:
    task = scheduler._event_task
    if task is not None:
        await asyncio.wait_for(task, timeout=5)


def test_off_by_default_does_nothing(monkeypatch) -> None:
    _settings(monkeypatch, delta_on_pr_event_enabled=False)
    calls = _stub_tick(monkeypatch)

    async def run():
        assert scheduler.request_delta_cycle("PR 1") == scheduler.EVENT_DISABLED
        await _drain()

    asyncio.run(run())
    assert calls == []


def test_no_database_means_unavailable_not_an_error(monkeypatch) -> None:
    _settings(monkeypatch, database_url="")
    calls = _stub_tick(monkeypatch)

    async def run():
        assert scheduler.request_delta_cycle("PR 1") == scheduler.EVENT_UNAVAILABLE

    asyncio.run(run())
    assert calls == []


def test_an_event_runs_one_cycle(monkeypatch) -> None:
    _settings(monkeypatch)
    calls = _stub_tick(monkeypatch)

    async def run():
        assert scheduler.request_delta_cycle("PR 1000000567") == scheduler.EVENT_SCHEDULED
        await _drain()

    asyncio.run(run())
    assert calls == ["event: PR 1000000567"]


def test_a_burst_shares_one_cycle(monkeypatch) -> None:
    """Events inside the debounce ride the cycle already scheduled."""
    _settings(monkeypatch, delta_event_debounce_seconds=1)
    calls = _stub_tick(monkeypatch)

    async def run():
        states = [scheduler.request_delta_cycle(f"PR {n}") for n in range(5)]
        assert states == [scheduler.EVENT_SCHEDULED] + [scheduler.EVENT_QUEUED] * 4
        await _drain()

    asyncio.run(run())
    assert len(calls) == 1


def test_an_event_during_the_cycle_gets_one_more(monkeypatch) -> None:
    """The running cycle may already have fetched the sets the new PR touches."""
    _settings(monkeypatch)
    calls = _stub_tick(monkeypatch, during=lambda: scheduler.request_delta_cycle("PR 2"))

    async def run():
        scheduler.request_delta_cycle("PR 1")
        await _drain()

    asyncio.run(run())
    assert len(calls) == 2
    assert calls[1] == "event: events during the last cycle"


def test_a_held_lock_is_retried_until_the_cycle_runs(monkeypatch) -> None:
    _settings(monkeypatch)
    calls = _stub_tick(monkeypatch, results=[False, False, True])

    async def run():
        scheduler.request_delta_cycle("PR 1")
        await _drain()

    asyncio.run(run())
    assert len(calls) == 3


def test_a_lock_that_never_frees_is_given_up_on(monkeypatch) -> None:
    _settings(monkeypatch)
    monkeypatch.setattr(scheduler, "EVENT_MAX_ATTEMPTS", 4)
    calls = _stub_tick(monkeypatch, results=[False] * 10)

    async def run():
        scheduler.request_delta_cycle("PR 1")
        await _drain()

    asyncio.run(run())
    assert len(calls) == 4


def test_a_new_event_after_the_cycle_schedules_again(monkeypatch) -> None:
    _settings(monkeypatch)
    calls = _stub_tick(monkeypatch)

    async def run():
        scheduler.request_delta_cycle("PR 1")
        await _drain()
        assert scheduler.request_delta_cycle("PR 2") == scheduler.EVENT_SCHEDULED
        await _drain()

    asyncio.run(run())
    assert calls == ["event: PR 1", "event: PR 2"]


def test_shutdown_cancels_a_pending_cycle(monkeypatch) -> None:
    _settings(monkeypatch, delta_event_debounce_seconds=60)
    calls = _stub_tick(monkeypatch)

    async def run():
        scheduler.request_delta_cycle("PR 1")
        await scheduler.stop_event_runner()
        assert scheduler._event_task is None

    asyncio.run(run())
    assert calls == []


def test_the_tick_reports_a_held_lock(monkeypatch) -> None:
    """The real _tick: a refused lock returns False, which is what the retry reads."""

    class _Held:
        def __init__(self, *a, **k):
            pass

        def acquire(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(scheduler, "_Lock", _Held)
    assert asyncio.run(scheduler._tick(trigger="event: PR 1")) is False


class TestTheEndpoint:
    def test_reports_disabled_by_default(self, monkeypatch) -> None:
        _settings(monkeypatch, delta_on_pr_event_enabled=False)
        response = TestClient(app).post("/api/events/pr", json={"BANFN": "1000000567"})
        assert response.status_code == 202
        assert response.json()["delta"] == scheduler.EVENT_DISABLED

    def test_asks_for_a_cycle_when_enabled(self, monkeypatch) -> None:
        asked: list[str] = []
        monkeypatch.setattr(scheduler, "request_delta_cycle",
                            lambda reason: asked.append(reason) or scheduler.EVENT_SCHEDULED)
        response = TestClient(app).post("/api/events/pr", json={"BANFN": "1000000567"})
        assert response.status_code == 202
        assert response.json()["delta"] == scheduler.EVENT_SCHEDULED
        assert asked == ["PR 1000000567"]

    def test_an_event_without_banfn_still_triggers(self, monkeypatch) -> None:
        asked: list[str] = []
        monkeypatch.setattr(scheduler, "request_delta_cycle",
                            lambda reason: asked.append(reason) or scheduler.EVENT_SCHEDULED)
        TestClient(app).post("/api/events/pr", json={"MESSAGE": "PR Created Successfully"})
        assert asked == ["PR (no BANFN)"]

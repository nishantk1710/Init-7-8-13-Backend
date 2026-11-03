"""The notification timer: locking, failure isolation and start-up rules."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from app.integrations.notifications import scheduler
from app.integrations.notifications.scheduler import Job, run_job, run_loop, run_once


class FakeLock:
    """The shape of ``app.ingest.scheduler._Lock``, in process."""

    held: set[str] = set()

    def __init__(self, name: str) -> None:
        self.name, self.mine = name, False

    def acquire(self) -> bool:
        if self.name in FakeLock.held:
            return False
        FakeLock.held.add(self.name)
        self.mine = True
        return True

    def release(self) -> None:
        FakeLock.held.discard(self.name)


def setup_function() -> None:
    FakeLock.held = set()


def test_a_job_runs_under_its_own_named_lock_and_releases_it():
    seen = []
    job = Job("approval_reminders", lambda: seen.append(set(FakeLock.held)) or "done")

    assert run_job(job, FakeLock) == "ran: done"

    assert seen == [{"spares_ai_notif_approval_reminders"}]
    assert FakeLock.held == set()


def test_a_job_whose_lock_is_held_by_another_worker_is_skipped():
    FakeLock.held = {"spares_ai_notif_deliver_pending"}
    calls = []

    assert run_job(Job("deliver_pending", lambda: calls.append(1) or "x"), FakeLock) == "skipped"

    assert calls == []
    assert FakeLock.held == {"spares_ai_notif_deliver_pending"}  # not released by the one that never held it


def test_two_overlapping_passes_cannot_both_run_the_same_job():
    results = []

    def slow() -> str:
        # while this job runs, a second worker tries the same job
        results.append(run_job(Job("same", lambda: "second"), FakeLock))
        return "first"

    assert run_job(Job("same", slow), FakeLock) == "ran: first"
    assert results == ["skipped"]


def test_a_failing_job_is_reported_releases_its_lock_and_does_not_stop_the_others():
    def broken() -> str:
        raise RuntimeError("boom")

    outcome = run_once([Job("a", broken), Job("b", lambda: "fine")], FakeLock)

    assert outcome == {"a": "failed", "b": "ran: fine"}
    assert FakeLock.held == set()


def test_a_lock_that_cannot_be_taken_is_a_failed_job_not_a_crash():
    class Down(FakeLock):
        def acquire(self) -> bool:
            raise ConnectionError("database unreachable")

    assert run_job(Job("a", lambda: "x"), Down) == "failed"


def test_the_loop_ticks_until_stopped():
    stop = threading.Event()
    ticks = []

    def job() -> str:
        ticks.append(1)
        if len(ticks) == 3:
            stop.set()
        return "ok"

    run_loop(stop, [Job("a", job)], 0.01, FakeLock)

    assert len(ticks) == 3


def settings(**overrides):
    fields = dict(notification_scheduler_enabled=True, notification_scheduler_interval_seconds=60)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_it_does_not_start_when_switched_off_or_with_nothing_to_run_or_a_silly_interval():
    stop = threading.Event()
    job = [Job("a", lambda: "x")]
    assert scheduler.start(stop, settings(notification_scheduler_enabled=False), job) is None
    assert scheduler.start(stop, settings(), []) is None
    assert scheduler.start(stop, settings(notification_scheduler_interval_seconds=1), job) is None


def test_it_starts_a_daemon_thread_that_stops_with_the_event():
    stop = threading.Event()

    thread = scheduler.start(stop, settings(), [Job("a", lambda: "x")])

    assert thread is not None and thread.daemon and thread.is_alive()
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_the_real_job_list_is_reminders_then_escalations_then_link_notices_then_delivery():
    from app.services.notification_jobs import build_jobs

    assert [job.name for job in build_jobs()] == [
        "approval_reminders",
        "requester_escalations",
        "request_link_notices",
        "deliver_pending",
    ]

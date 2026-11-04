"""I13 exception detection on a schedule: the nightly job (off by default, once a
day, after its hour) and the run after each successful snapshot build or re-age
(``I13_DETECT_AFTER_BUILD``), both limited to ``I13_DETECTION_TYPES``."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.config import get_settings
from app.initiatives.i13.act.service import DetectionRunResult
from app.services import notification_jobs

NIGHT = datetime(2026, 11, 4, 1, 5, tzinfo=timezone.utc)


class FakeSession:
    committed = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        FakeSession.committed += 1


@pytest.fixture(autouse=True)
def fresh_job(monkeypatch):
    monkeypatch.setattr(notification_jobs, "_last_detection_date", None)
    FakeSession.committed = 0


@pytest.fixture
def runs(monkeypatch):
    seen: list[dict] = []

    def fake_run(db, config, data_dir, *, as_of_time, snapshot=None, material=None, plant=None, types=None):
        seen.append({"as_of_time": as_of_time, "snapshot": snapshot, "material": material, "plant": plant, "types": types})
        return DetectionRunResult(as_of_time=as_of_time, created=2, reused=5, resolved=1, routed=2)

    monkeypatch.setattr("app.initiatives.i13.act_runner.run_detection", fake_run)
    return seen


def enabled(**update):
    # Snapshot off: the job takes the live path and never touches a snapshot here.
    return get_settings().model_copy(
        update={"notification_i13_detection_enabled": True, "notification_i13_detection_hour_utc": 1, "i13_snapshot_enabled": False, **update}
    )


def test_off_by_default_and_separate_from_the_scheduler_switch():
    from app.core.config import Settings

    assert Settings.model_fields["notification_i13_detection_enabled"].default is False
    assert get_settings().model_copy(update={"notification_scheduler_enabled": True}).notification_i13_detection_enabled is False


def test_disabled_does_not_touch_the_database():
    def must_not_open_a_session():
        raise AssertionError("a disabled job must not touch the database")

    settings = get_settings().model_copy(update={"notification_i13_detection_enabled": False})
    assert notification_jobs.run_exception_detection(settings, session_factory=must_not_open_a_session, now=NIGHT).startswith("disabled")


def test_waits_for_its_hour(runs):
    early = NIGHT.replace(hour=0)
    assert notification_jobs.run_exception_detection(enabled(), session_factory=FakeSession, now=early) == "waiting for 01:00 UTC"
    assert runs == []


def test_runs_over_every_material_once_a_day(runs):
    outcome = notification_jobs.run_exception_detection(enabled(), session_factory=FakeSession, now=NIGHT)

    assert outcome == "2 created, 5 kept, 1 resolved, 2 routed (all types)"
    assert runs == [{"as_of_time": NIGHT, "snapshot": None, "material": None, "plant": None, "types": None}]
    assert FakeSession.committed == 1

    later = NIGHT.replace(hour=14)
    assert notification_jobs.run_exception_detection(enabled(), session_factory=FakeSession, now=later) == "already ran today"
    assert len(runs) == 1

    tomorrow = NIGHT.replace(day=5)
    notification_jobs.run_exception_detection(enabled(), session_factory=FakeSession, now=tomorrow)
    assert len(runs) == 2


def test_a_missed_hour_runs_on_the_next_tick_that_day(runs):
    """A restart across the hour must not skip the day."""
    notification_jobs.run_exception_detection(enabled(), session_factory=FakeSession, now=NIGHT.replace(hour=9))
    assert len(runs) == 1


def test_skips_while_the_snapshot_is_not_ready_and_retries(monkeypatch, runs):
    from app.initiatives.i13.snapshot import SnapshotBuilding
    from app.initiatives.i13.snapshot_store import lifecycle

    def building(db):
        raise SnapshotBuilding(NIGHT)

    monkeypatch.setattr(lifecycle, "enabled", lambda: True)
    monkeypatch.setattr(lifecycle, "current", building)

    outcome = notification_jobs.run_exception_detection(enabled(i13_snapshot_enabled=True), session_factory=FakeSession, now=NIGHT)

    assert outcome.startswith("skipped: I13 snapshot not available")
    assert runs == []
    assert notification_jobs._last_detection_date is None  # not marked done: the next tick tries again


def test_a_failed_run_is_logged_raised_and_tried_again(monkeypatch, caplog):
    def failing(*args, **kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr("app.initiatives.i13.act_runner.run_detection", failing)

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError):
        notification_jobs.run_exception_detection(enabled(), session_factory=FakeSession, now=NIGHT)

    assert "nothing from this run was saved" in caplog.text
    assert FakeSession.committed == 0
    assert notification_jobs._last_detection_date is None


def test_an_hour_outside_the_day_is_refused(runs):
    outcome = notification_jobs.run_exception_detection(enabled(notification_i13_detection_hour_utc=25), session_factory=FakeSession, now=NIGHT)
    assert outcome == "skipped: invalid hour"
    assert runs == []


# --- 4-Nov-2026: the types it checks, and the run after a build ----------------


def test_checks_only_the_configured_types(runs):
    from app.initiatives.i13.act.domain import ExceptionType

    outcome = notification_jobs.run_exception_detection(
        enabled(i13_detection_types="plan_breach, NO_PLAN_GRNI"), session_factory=FakeSession, now=NIGHT
    )

    assert runs[0]["types"] == frozenset({ExceptionType.PLAN_BREACH, ExceptionType.NO_PLAN_GRNI})
    assert outcome.endswith("(NO_PLAN_GRNI, PLAN_BREACH)")


def test_an_unknown_type_is_refused_not_ignored(runs):
    with pytest.raises(ValueError, match="NO_PLANS"):
        notification_jobs.run_exception_detection(enabled(i13_detection_types="NO_PLANS"), session_factory=FakeSession, now=NIGHT)
    assert runs == []


def test_after_build_is_off_by_default():
    from app.core.config import Settings

    assert Settings.model_fields["i13_detect_after_build"].default is False

    def must_not_open_a_session():
        raise AssertionError("a disabled run must not touch the database")

    settings = get_settings().model_copy(update={"i13_detect_after_build": False})
    assert notification_jobs.detect_after_build("test", settings, session_factory=must_not_open_a_session).startswith("disabled")


def test_after_build_runs_at_any_hour_and_counts_as_the_days_run(runs):
    settings = enabled(i13_detect_after_build=True, i13_detection_types="PLAN_BREACH")
    early = NIGHT.replace(hour=0)  # before the nightly hour: the build decides, not the clock

    notification_jobs.detect_after_build("re-age", settings, session_factory=FakeSession, now=early)
    notification_jobs.detect_after_build("build", settings, session_factory=FakeSession, now=early.replace(minute=40))

    assert len(runs) == 2  # every successful build, not once a day
    assert FakeSession.committed == 2
    # The nightly backup sees the day as done.
    assert notification_jobs.run_exception_detection(settings, session_factory=FakeSession, now=NIGHT) == "already ran today"
    assert len(runs) == 2


def test_a_successful_build_hands_over_to_detection(monkeypatch):
    from app.initiatives.i13.snapshot_store import lifecycle

    started: list[str] = []
    monkeypatch.setattr(lifecycle, "_start_detection_after_build", started.append)
    monkeypatch.setattr(lifecycle, "_lock_held", lambda: False)

    class Process:
        def __init__(self, code):
            self.code = code

        def wait(self):
            return self.code

    for code in (0, 1, 3):
        monkeypatch.setattr(lifecycle.subprocess, "Popen", lambda *a, code=code, **k: Process(code))
        monkeypatch.setattr(lifecycle, "_last_failure", lambda db, running: None)
        lifecycle._launch_and_wait(f"exit {code}", after=None, manual=True)

    assert started == ["exit 0"]  # not after a failed build, nor one another process held

"""H3: I13 automatic escalation is safe by default.

M4 (I08 shared notifications are recoverable) was tested here too until
3-Nov-2026, when the I08 exception queue and its notifications were removed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.config import get_settings
from app.initiatives.i13.act.domain import ActException, ExceptionStatus
from app.initiatives.i13.act.service import process_escalations
from app.initiatives.i13.act_hod_provider import ConfigEscalationRecipientProvider
from app.integrations.notifications.service import SharedNotificationPort
from app.models.base import Base
from tests.notifications.test_i13_shared import (  # noqa: F401 -- fixtures and helpers shared with that module
    HOD,
    NOW,
    OID,
    act_exception,
    repository,
    shared,
)


# --- H3: I13 automatic escalation is safe by default -------------------------


def overdue(n: int, plant: str = "1300", days: int = 1) -> ActException:
    return act_exception(
        exception_id=f"ACT-{n}", plant=plant, requester_due_at=NOW - timedelta(days=days, minutes=-n),
        owner_requester_id=OID, current_assignee_id=OID,
    )


def test_the_default_for_automatic_escalation_is_off_and_it_has_its_own_setting():
    from app.core.config import Settings

    assert Settings.model_fields["notification_i13_escalation_enabled"].default is False
    assert Settings.model_fields["notification_i13_escalation_batch_size"].default >= 1
    assert "notification_scheduler_enabled" in Settings.model_fields  # the scheduler has its own switch


def test_the_scheduler_being_on_does_not_turn_escalation_on():
    settings = get_settings().model_copy(update={"notification_scheduler_enabled": True})
    assert settings.notification_i13_escalation_enabled is False


def test_the_job_does_nothing_when_disabled():
    from app.services.notification_jobs import run_requester_escalations

    def must_not_open_a_session():
        raise AssertionError("a disabled job must not touch the database")

    settings = get_settings().model_copy(
        update={"notification_scheduler_enabled": True, "notification_i13_escalation_enabled": False, "i13_hod_recipients": f"1300:{HOD}"}
    )

    assert run_requester_escalations(settings, session_factory=must_not_open_a_session).startswith("disabled")


def test_enabled_without_a_hod_configured_skips_instead_of_changing_statuses(caplog):
    from app.services.notification_jobs import run_requester_escalations

    def no_session():
        raise AssertionError("no session should be opened")

    settings = get_settings().model_copy(update={"notification_i13_escalation_enabled": True, "i13_hod_recipients": ""})

    with caplog.at_level("WARNING"):
        outcome = run_requester_escalations(settings, session_factory=no_session)

    assert outcome == "skipped: no HOD configured"
    assert "nobody to escalate to" in caplog.text


def test_the_job_passes_the_batch_limit_and_only_plants_with_a_hod(monkeypatch, caplog):
    from app.initiatives.i13.act.service import EscalationRunResult
    from app.services import notification_jobs

    seen = {}

    def fake_run(db, config, *, as_of_time, limit=None, plants=None):
        seen.update(limit=limit, plants=set(plants))
        return EscalationRunResult(as_of_time=as_of_time, escalated=limit, routing_pending=0)

    class FakeSession:
        committed = False

        def commit(self):
            FakeSession.committed = True

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("app.initiatives.i13.act_runner.run_escalations", fake_run)
    settings = get_settings().model_copy(
        update={
            "notification_i13_escalation_enabled": True,
            "notification_i13_escalation_batch_size": 3,
            "i13_hod_recipients": f"1300:{HOD},1500:{OID}",
        }
    )

    with caplog.at_level("INFO"):
        outcome = notification_jobs.run_requester_escalations(settings, session_factory=FakeSession)

    assert seen == {"limit": 3, "plants": {"1300", "1500"}}
    assert FakeSession.committed
    assert outcome.startswith("3 processed") and "batch limit 3" in outcome
    assert "batch of 3 full" in caplog.text  # a full batch is logged, so a backlog is visible


def test_a_failed_batch_is_logged_and_raised_not_swallowed(monkeypatch, caplog):
    from app.services import notification_jobs

    def failing(*args, **kwargs):
        raise RuntimeError("database down")

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def commit(self):
            raise AssertionError("must not commit a failed batch")

    monkeypatch.setattr("app.initiatives.i13.act_runner.run_escalations", failing)
    settings = get_settings().model_copy(update={"notification_i13_escalation_enabled": True, "i13_hod_recipients": f"1300:{HOD}"})

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError):
        notification_jobs.run_requester_escalations(settings, session_factory=FakeSession)

    assert "nothing from this batch was saved" in caplog.text  # run_job then records the job as failed


def run_batch(db, repo, *, limit=None, plants=None):
    return process_escalations(
        NOW,
        repository=repo,
        escalation_recipient_provider=ConfigEscalationRecipientProvider.from_config_string(f"1300:{HOD},1500:{OID}"),
        notification_port=SharedNotificationPort(db),
        limit=limit,
        plants=plants,
    )


def test_a_run_takes_at_most_the_batch_limit_oldest_overdue_first(db):
    repo = repository(*[overdue(n, days=10 - n) for n in range(1, 6)])  # ACT-1 is the most overdue

    first = run_batch(db, repo, limit=2)

    assert first.escalated == 2
    assert {e.exception_id for e in repo.list() if e.status is ExceptionStatus.ESCALATED} == {"ACT-1", "ACT-2"}
    assert {e.exception_id for e in repo.list(status=ExceptionStatus.AWAITING_REQUESTER)} == {"ACT-3", "ACT-4", "ACT-5"}


def test_successive_runs_work_through_the_backlog_and_then_stop(db):
    repo = repository(*[overdue(n, days=10 - n) for n in range(1, 6)])

    counts = [run_batch(db, repo, limit=2).escalated for _ in range(4)]

    assert counts == [2, 2, 1, 0]
    assert repo.list(status=ExceptionStatus.AWAITING_REQUESTER) == []


def test_without_a_limit_every_overdue_exception_is_escalated_as_before(db):
    repo = repository(*[overdue(n) for n in range(1, 6)])
    assert run_batch(db, repo).escalated == 5  # the manual endpoint's behaviour is unchanged


def test_an_automatic_run_leaves_exceptions_at_plants_without_a_hod_untouched(db):
    repo = repository(overdue(1, plant="1300"), overdue(2, plant="1700"))  # no HOD for 1700

    result = run_batch(db, repo, limit=10, plants={"1300"})

    assert (result.escalated, result.routing_pending) == (1, 0)
    assert repo.get("ACT-2").status is ExceptionStatus.AWAITING_REQUESTER  # not moved to "escalated" with nobody to tell


def test_exceptions_not_yet_due_are_never_taken_whatever_the_limit(db):
    repo = repository(overdue(1), act_exception(exception_id="ACT-LATER", requester_due_at=NOW + timedelta(days=1)))
    assert run_batch(db, repo, limit=10).escalated == 1


def test_the_sql_repository_filters_and_limits_in_the_database(db):
    from app.initiatives.i13.act_exception_store import SqlExceptionRepository
    from app.models.i13_act_exception import ActExceptionRecord

    Base.metadata.create_all(db.get_bind(), tables=[ActExceptionRecord.__table__])
    sql = SqlExceptionRepository(db)
    for n, plant in [(1, "1300"), (2, "1300"), (3, "1700"), (4, "1300")]:
        sql.upsert(overdue(n, plant=plant, days=10 - n))
    sql.upsert(act_exception(exception_id="ACT-LATER", requester_due_at=NOW + timedelta(days=1)))
    db.commit()

    taken = sql.list_due_for_escalation(NOW, plants={"1300"}, limit=2)

    assert [e.exception_id for e in taken] == ["ACT-1", "ACT-2"]  # oldest due first, only 1300, at most 2
    assert len(sql.list_due_for_escalation(NOW)) == 4  # no filters: every overdue one, but not the not-yet-due one

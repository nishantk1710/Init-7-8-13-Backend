"""M4 (I08 shared notifications are recoverable) and H3 (I13 automatic escalation is safe by default)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.initiatives.i13.act.domain import ActException, ExceptionStatus
from app.initiatives.i13.act.service import process_escalations
from app.initiatives.i13.act_hod_provider import ConfigEscalationRecipientProvider
from app.initiatives.i8.models import Notification
from app.initiatives.i8.notifications import log_new
from app.integrations.notifications.service import SharedNotificationPort
from app.models.app_user import AppUser
from app.models.base import Base
from app.models.notification import NotificationRecipient
from tests.notifications.test_i08_and_i13 import (  # noqa: F401 -- fixtures and helpers shared with that module
    HOD,
    NOW,
    OID,
    act_exception,
    cfg,
    i8_db,
    item,
    repository,
    shared,
)


# --- M4: I08 shared notifications are recoverable ----------------------------


def logged(db):
    return db.scalar(select(func.count()).select_from(Notification))


def stores(db, monkeypatch, roles="OVERDUE_REPAIR:Spares.Stores,MISSING_ATTESTATION:Spares.Stores"):
    monkeypatch.setattr(get_settings(), "i8_notification_recipient_roles", roles)
    if roles:
        db.add(AppUser(oid=OID, name="Stores", email="stores@example.com"))
        db.add(NotificationRecipient(role="Spares.Stores", plant="1300", user_oid=OID, active=True))
        db.commit()


def inbox_for(db, exception_id):
    return [r for r in shared(db) if r.channel == "IN_APP" and r.entity_id == exception_id]


def refuse_notification_inserts(db):
    db.execute(text("CREATE TRIGGER refuse_notification BEFORE INSERT ON notification BEGIN SELECT RAISE(ABORT, 'refused'); END"))
    db.commit()


def test_a_shared_queue_failure_keeps_the_log_and_the_next_read_queues_the_item(i8_db, cfg, monkeypatch):
    stores(i8_db, monkeypatch)
    real = log_new.__globals__["enqueue"]
    monkeypatch.setattr("app.initiatives.i8.notifications.enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))

    assert log_new(i8_db, [item(1)], cfg) == 1  # the log row is written
    assert logged(i8_db) == 1 and shared(i8_db) == []  # the shared notification is not

    monkeypatch.setattr("app.initiatives.i8.notifications.enqueue", real)
    assert log_new(i8_db, [item(1)], cfg) == 0  # nothing NEW to log...

    assert logged(i8_db) == 1  # ...the log is not duplicated
    assert len(inbox_for(i8_db, item(1).id)) == 1  # ...but the missed notification was recovered


def test_a_database_level_failure_inside_the_shared_queue_does_not_cost_the_log_row(i8_db, cfg, monkeypatch):
    stores(i8_db, monkeypatch)
    refuse_notification_inserts(i8_db)

    assert log_new(i8_db, [item(1), item(2)], cfg) == 2

    i8_db.expire_all()
    assert logged(i8_db) == 2 and shared(i8_db) == []
    i8_db.execute(text("DROP TRIGGER refuse_notification"))
    i8_db.commit()
    log_new(i8_db, [item(1), item(2)], cfg)
    assert len(inbox_for(i8_db, item(1).id)) == 1 and len(inbox_for(i8_db, item(2).id)) == 1


def test_the_failure_is_logged_and_says_it_will_be_retried(i8_db, cfg, monkeypatch, caplog):
    stores(i8_db, monkeypatch)
    monkeypatch.setattr("app.initiatives.i8.notifications.enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))

    with caplog.at_level("WARNING"):
        log_new(i8_db, [item(1)], cfg)

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "could not queue 1 shared notification" in messages
    assert "queued again on the next read" in messages and "RuntimeError" in messages


def test_items_logged_while_roles_were_empty_are_processed_once_roles_are_configured(i8_db, cfg, monkeypatch):
    monkeypatch.setattr(get_settings(), "i8_notification_recipient_roles", "")
    assert log_new(i8_db, [item(1), item(2)], cfg) == 2
    assert shared(i8_db) == [] and logged(i8_db) == 2

    stores(i8_db, monkeypatch)  # roles configured later
    assert log_new(i8_db, [item(1), item(2)], cfg) == 0  # no new log rows

    assert logged(i8_db) == 2
    assert len(inbox_for(i8_db, item(1).id)) == 1 and len(inbox_for(i8_db, item(2).id)) == 1


def test_repeated_reads_add_nothing(i8_db, cfg, monkeypatch):
    stores(i8_db, monkeypatch)
    log_new(i8_db, [item(1), item(2)], cfg)
    after_first = len(shared(i8_db))

    for _ in range(3):
        assert log_new(i8_db, [item(1), item(2)], cfg) == 0

    assert len(shared(i8_db)) == after_first and logged(i8_db) == 2


def test_an_item_that_already_has_a_shared_notification_is_not_queued_again(i8_db, cfg, monkeypatch):
    stores(i8_db, monkeypatch)
    log_new(i8_db, [item(1)], cfg)
    keys_before = {r.idempotency_key for r in shared(i8_db)}

    # the log row is lost but the shared notification remains: the idempotency key still prevents a repeat
    i8_db.execute(text("DELETE FROM i8_notification"))
    i8_db.commit()
    log_new(i8_db, [item(1)], cfg)

    assert {r.idempotency_key for r in shared(i8_db)} == keys_before


def test_recovery_works_through_a_backlog_a_batch_at_a_time(i8_db, cfg, monkeypatch):
    monkeypatch.setattr("app.initiatives.i8.notifications.RECOVERY_BATCH", 2)
    monkeypatch.setattr(get_settings(), "i8_notification_recipient_roles", "")
    items = [item(n) for n in range(1, 6)]
    log_new(i8_db, items, cfg)
    stores(i8_db, monkeypatch)

    done = []
    for _ in range(4):
        log_new(i8_db, items, cfg)
        done.append(len({r.entity_id for r in shared(i8_db) if r.channel == "IN_APP"}))

    assert done == [2, 4, 5, 5]  # two per read, then nothing left to do


def test_only_current_items_of_a_mapped_type_are_recovered(i8_db, cfg, monkeypatch):
    monkeypatch.setattr(get_settings(), "i8_notification_recipient_roles", "")
    log_new(i8_db, [item(1), item(2, "MISSING_ATTESTATION")], cfg)
    stores(i8_db, monkeypatch, roles="OVERDUE_REPAIR:Spares.Stores")  # MISSING_ATTESTATION is not mapped

    log_new(i8_db, [item(2, "MISSING_ATTESTATION")], cfg)  # item 1 is no longer in the queue (resolved)

    assert shared(i8_db) == []


def test_recovery_respects_the_feature_flag(i8_db, cfg, monkeypatch):
    stores(i8_db, monkeypatch)
    off = cfg.model_copy(update={"complete_flow_enabled": False})

    assert log_new(i8_db, [item(1)], off) == 0

    assert shared(i8_db) == [] and logged(i8_db) == 0


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

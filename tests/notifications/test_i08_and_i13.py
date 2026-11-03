"""I08 and I13 through the shared service.

I08: the existing exception log still works, and Repairable Spares sends no in-app or email notification to
anyone. I13: the requester and HOD notifications land in the shared table, and escalation runs through the same
code the scheduler calls.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.initiatives.i13.act.domain import (
    ActException,
    AssigneeType,
    ExceptionStatus,
    ExceptionType,
    NotificationChannel,
    RoutingStatus,
)
from app.initiatives.i13.act.service import process_escalations
from app.initiatives.i13.act_hod_provider import ConfigEscalationRecipientProvider
from app.initiatives.i8 import notifications as i8_notifications
from app.initiatives.i8.config import get_i8_settings
from app.initiatives.i8.exceptions import ExceptionItem
from app.initiatives.i8.models import Notification
from app.initiatives.i8.notifications import log_new
from app.integrations.notifications.service import SharedNotificationPort
from app.models.app_user import AppUser
from app.models.base import Base
from app.models.notification import NotificationRecipient, NotificationRow
from tests.i13.conftest import FakeExceptionRepository

OID = "11111111-1111-1111-1111-111111111111"
HOD = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def i8_db(db):
    Base.metadata.create_all(db.get_bind(), tables=[Notification.__table__])
    return db


def item(n: int = 1, type: str = "OVERDUE_REPAIR", plant: str = "1300") -> ExceptionItem:
    return ExceptionItem(
        id=f"EX-{type}-45000000{n:02d}-10", type=type, severity="warning", material_id=f"80000000{n:02d}",
        description="Pump seal", plant=plant, purchasing_document=f"45000000{n:02d}", item="10",
        title="Repair is overdue", detail="Promised back on 1 Oct.", raised_at=date(2026, 9, 1),
    )


@pytest.fixture
def cfg():
    return get_i8_settings().model_copy(update={"notification_types": "OVERDUE_REPAIR,MISSING_ATTESTATION", "complete_flow_enabled": True})


def shared(db):
    return list(db.scalars(select(NotificationRow).order_by(NotificationRow.idempotency_key)))


# --- I08 ---------------------------------------------------------------------


def someone_is_set_up_to_hear_about_repairs(db):
    """Everything that used to make Repairable Spares notify somebody: a user, and a role row for the plant."""
    db.add(AppUser(oid=OID, name="Stores", email="stores@example.com"))
    db.add(NotificationRecipient(role="Spares.Stores", plant="1300", user_oid=OID, active=True))
    db.commit()


def test_i08_log_is_written_and_nothing_goes_to_the_inbox_or_email(i8_db, cfg):
    someone_is_set_up_to_hear_about_repairs(i8_db)

    assert log_new(i8_db, [item(1), item(2, plant="1500")], cfg) == 2

    assert i8_db.scalar(select(Notification.exception_id).limit(1)) is not None  # the exception log is still written
    assert shared(i8_db) == []  # no in-app notification, no email, not even a recorded failure


def test_i08_has_no_way_left_to_queue_a_shared_notification():
    for name in ("enqueue", "resolve_role", "_queue_shared", "_roles_by_type", "_without_shared_notification"):
        assert not hasattr(i8_notifications, name), f"Repairable Spares must not reach the shared notification service: {name}"


def test_i08_reading_the_queue_again_adds_nothing(i8_db, cfg):
    assert log_new(i8_db, [item(1)], cfg) == 1
    assert log_new(i8_db, [item(1)], cfg) == 0

    assert len(list(i8_db.scalars(select(Notification)))) == 1
    assert shared(i8_db) == []


def test_i08_flag_off_writes_nothing_at_all(i8_db, cfg):
    someone_is_set_up_to_hear_about_repairs(i8_db)

    assert log_new(i8_db, [item(1)], cfg.model_copy(update={"complete_flow_enabled": False})) == 0

    assert shared(i8_db) == []
    assert list(i8_db.scalars(select(Notification))) == []


def test_i08_types_not_configured_for_the_log_are_not_logged(i8_db, cfg):
    log_new(i8_db, [item(1, "OVERDUE_REPAIR"), item(2, "REPAIR_PR_NOT_DECLARED")], cfg.model_copy(update={"notification_types": "OVERDUE_REPAIR"}))

    assert [row.exception_type for row in i8_db.scalars(select(Notification))] == ["OVERDUE_REPAIR"]


# --- I13 ---------------------------------------------------------------------


NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


def act_exception(**overrides) -> ActException:
    fields = dict(
        exception_id="ACT-1", exception_type=ExceptionType.NO_PLAN, status=ExceptionStatus.AWAITING_REQUESTER,
        material="5000092261", plant="1300", reservation_number="R1", reservation_item="1", session_id=None,
        ledger_entry_id=None, owner_requester_id=OID, detected_at=NOW - timedelta(days=9),
        requester_due_at=NOW - timedelta(days=1), escalated_at=None, resolved_at=None,
        current_assignee_type=AssigneeType.REQUESTER, current_assignee_id=OID, routing_status=RoutingStatus.RESOLVED,
        reason="No consumption plan for the reservation.",
    )
    fields.update(overrides)
    return ActException(**fields)


def repository(*exceptions) -> FakeExceptionRepository:
    repo = FakeExceptionRepository()
    for exception in exceptions:
        repo.upsert(exception)
    return repo


def test_i13_hod_escalation_reaches_the_hod_through_the_shared_service(db):
    db.add(AppUser(oid=HOD, name="HOD", email="hod@example.com"))
    db.commit()
    repo = repository(act_exception())

    result = process_escalations(
        NOW,
        repository=repo,
        escalation_recipient_provider=ConfigEscalationRecipientProvider.from_config_string(f"1300:{HOD}"),
        notification_port=SharedNotificationPort(db),
    )
    db.commit()

    assert result.escalated == 1
    rows = shared(db)
    assert {(r.channel, r.recipient_user_id, r.recipient_email) for r in rows} == {
        ("IN_APP", HOD, "hod@example.com"),
        ("EMAIL", HOD, "hod@example.com"),
    }
    inbox = next(r for r in rows if r.channel == "IN_APP")
    assert inbox.title == "Escalation Required – Exception Not Confirmed"
    assert inbox.status == "SENT" and next(r for r in rows if r.channel == "EMAIL").status == "PENDING"
    assert "I13" not in inbox.title + inbox.description and "ACT" not in inbox.title + inbox.description
    # I13's own audit trail is still written, and never claims delivery for the email
    assert {a.channel for a in repo.list_notifications("ACT-1")} == {NotificationChannel.PLATFORM_QUEUE, NotificationChannel.EMAIL}
    assert [a.outcome.value for a in repo.list_notifications("ACT-1") if a.channel is NotificationChannel.EMAIL] == ["LOGGED_ONLY"]


def test_i13_escalating_twice_does_not_notify_twice(db):
    first = repository(act_exception())
    again = repository(act_exception())
    for repo in (first, again):
        process_escalations(
            NOW, repository=repo,
            escalation_recipient_provider=ConfigEscalationRecipientProvider.from_config_string(f"1300:{HOD}"),
            notification_port=SharedNotificationPort(db),
        )
    db.commit()

    assert len([r for r in shared(db) if r.channel == "IN_APP"]) == 1


def test_i13_with_no_hod_configured_still_escalates_the_status_and_notifies_nobody(db):
    repo = repository(act_exception())

    result = process_escalations(
        NOW, repository=repo,
        escalation_recipient_provider=ConfigEscalationRecipientProvider.from_config_string(""),
        notification_port=SharedNotificationPort(db),
    )

    assert (result.escalated, result.routing_pending) == (0, 1)
    assert repo.get("ACT-1").status is ExceptionStatus.ESCALATED  # unchanged behaviour
    assert shared(db) == []


def test_i13_not_yet_due_exception_is_left_alone(db):
    repo = repository(act_exception(requester_due_at=NOW + timedelta(days=2)))
    result = process_escalations(
        NOW, repository=repo,
        escalation_recipient_provider=ConfigEscalationRecipientProvider.from_config_string(f"1300:{HOD}"),
        notification_port=SharedNotificationPort(db),
    )
    assert result.escalated == 0 and shared(db) == []

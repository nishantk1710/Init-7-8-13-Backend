"""I13 through the shared service: the requester and HOD notifications land in
the shared table, and escalation runs through the same code the scheduler calls.

I08's exception notifications were tested here too until 3-Nov-2026, when the
I08 exception queue and its notifications were removed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

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
from app.integrations.notifications.service import SharedNotificationPort
from app.models.app_user import AppUser
from app.models.notification import NotificationRow
from tests.i13.conftest import FakeExceptionRepository

OID = "11111111-1111-1111-1111-111111111111"
HOD = "22222222-2222-2222-2222-222222222222"


def shared(db):
    return list(db.scalars(select(NotificationRow).order_by(NotificationRow.idempotency_key)))


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
    assert inbox.title == "Escalation Required – Inventory Exception"
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

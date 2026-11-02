"""NOTIFICATION_EMAIL_ENABLED=false: the in-app inbox is the only channel.

With the switch off, no email row is created and nothing is sent, but every in-app
notification works as before and a notification that reaches nobody is still recorded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.initiatives.i7.recommendations.types import ApprovalRole
from app.initiatives.i7.reminders import run_reminders
from app.integrations.notifications import service
from app.integrations.notifications.resolver import resolve_user
from app.integrations.notifications.models import (
    Channel,
    NotificationChannel,
    NotificationEvent,
    NotificationIntent,
    NotificationOutcome,
    Recipient,
)
from app.models.app_user import AppUser
from app.models.notification import NotificationRow, NotificationSla
from tests.notifications.test_i07_approvals import SUBMITTER, act, configure, person, recommendation, submit

OID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def email_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "notification_email_enabled", False)


def all_rows(db):
    return list(db.scalars(select(NotificationRow)))


def event(**overrides):
    fields = dict(module="I07", event_type="APPROVAL_REQUIRED", entity_type="RECOMMENDATION", entity_id="R1", trigger_id="1")
    fields.update(overrides)
    return NotificationEvent(**fields)


def test_email_is_on_by_default_so_nothing_changes_until_it_is_switched_off():
    assert Settings.model_fields["notification_email_enabled"].default is True


def test_an_approval_creates_only_the_in_app_notification(db, api, email_off):
    configure(db)
    submit(api, recommendation(db))

    rows = all_rows(db)
    assert rows and {r.channel for r in rows} == {"IN_APP"}
    inbox = api.get("/api/notifications", headers={"X-Actor-Id": person(ApprovalRole.END_USER)}).json()
    assert [i["title"] for i in inbox["items"]] == ["Approval Required – Material Recommendation"]


def test_every_approval_event_stays_in_app_only(db, api, email_off):
    configure(db)
    rec = recommendation(db)
    submit(api, rec)
    act(api, rec, ApprovalRole.END_USER, "APPROVE")
    act(api, rec, ApprovalRole.ENGINEERING_MANAGER, "SEND_BACK", comment="Check the lead time")
    act(api, rec, ApprovalRole.END_USER, "HOLD", comment="Waiting")
    act(api, rec, ApprovalRole.END_USER, "RELEASE_HOLD", comment="Back")
    act(api, rec, ApprovalRole.END_USER, "REJECT", comment="No")

    assert {r.channel for r in all_rows(db)} == {"IN_APP"}


def test_an_approver_nobody_configured_is_still_recorded_but_not_as_an_email(db, api, email_off):
    submit(api, recommendation(db))  # no recipients at all

    [failure] = all_rows(db)
    assert failure.channel == "IN_APP" and failure.status == "FAILED" and "Spares.EndUser" in failure.error
    assert failure.recipient_user_id is None  # no inbox shows it
    assert api.get("/api/notifications", headers={"X-Actor-Id": SUBMITTER}).json()["total"] == 0


def test_a_known_user_with_no_email_address_is_not_a_failure_when_email_is_off(db, email_off):
    db.add(AppUser(oid=OID, name="No email", email=""))
    [recipient] = resolve_user(db, OID)
    assert recipient.email_error  # the resolver still reports it...

    service.enqueue(db, event(), [recipient])
    db.commit()

    assert [(r.channel, r.status) for r in all_rows(db)] == [("IN_APP", "SENT")]  # ...but it is not a failed email


def test_the_switch_can_also_be_given_directly_and_leaves_email_on_otherwise(db):
    recipient = Recipient(user_oid=OID, email="a@example.com")

    service.enqueue(db, event(trigger_id="off"), [recipient], email_enabled=False)
    service.enqueue(db, event(trigger_id="on"), [recipient], email_enabled=True)
    db.commit()

    by_trigger = {(r.idempotency_key.split("|")[2], r.channel) for r in all_rows(db)}
    assert by_trigger == {("off", "IN_APP"), ("on", "IN_APP"), ("on", "EMAIL")}


def test_waiting_email_rows_are_not_sent_while_email_is_off(db, email_off):
    db.add(
        NotificationRow(
            idempotency_key="k1", module="I07", event_type="APPROVAL_REQUIRED", channel="EMAIL", recipient_email="a@example.com",
            title="t", description="d", status="PENDING", is_read=False, retry_count=0,
            next_attempt_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
    )
    db.commit()

    result = service.dispatch_with_settings(db)

    assert (result.sent, result.logged_only, result.failed) == (0, 0, 0)
    assert db.scalar(select(NotificationRow.status)) == "PENDING"  # untouched


def test_i13_email_intents_do_nothing_but_its_in_app_notification_still_works(db, email_off):
    db.add(AppUser(oid=OID, name="Requester", email="req@example.com"))
    db.commit()
    port = service.SharedNotificationPort(db)
    base = dict(exception_id="EX-1", recipient=OID, subject="s", body="b", module="I13", plant="1300")

    email = port.send(NotificationIntent(channel=NotificationChannel.EMAIL, **base))
    in_app = port.send(NotificationIntent(channel=NotificationChannel.PLATFORM_QUEUE, **base))
    db.commit()

    assert email.outcome is NotificationOutcome.LOGGED_ONLY and "switched off" in email.detail
    assert in_app.outcome is NotificationOutcome.SENT
    assert [(r.channel, r.status) for r in all_rows(db)] == [("IN_APP", "SENT")]


def test_reminders_are_in_app_only_too(db, api, email_off):
    configure(db)
    db.add(NotificationSla(approval_route="ALL", first_reminder_minutes=1, repeat_minutes=5, max_reminders=1, active=True))
    db.commit()
    submit(api, recommendation(db))

    run_reminders(db, datetime.now(timezone.utc) + timedelta(minutes=5))
    db.commit()

    rows = [r for r in all_rows(db) if r.event_type == "APPROVAL_REMINDER"]
    assert rows and {r.channel for r in rows} == {"IN_APP"}

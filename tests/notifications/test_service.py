"""The shared service: the outbox, delivery, retries and the Graph adapter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.core.config import get_settings
from app.integrations.notifications import service, templates
from app.integrations.notifications.graph_adapter import GraphEmailAdapter, UnconfiguredEmailAdapter
from app.integrations.notifications.logging_adapter import LoggingNotificationAdapter
from app.integrations.notifications.models import (
    Channel,
    NotificationChannel,
    NotificationEvent,
    NotificationIntent,
    NotificationOutcome,
    NotificationResult,
    Recipient,
    Status,
)
from app.models.app_user import AppUser
from app.models.notification import NotificationRow

OID = "11111111-1111-1111-1111-111111111111"
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
BASE = "https://spares.example.com"


def event(**overrides) -> NotificationEvent:
    fields = dict(
        module="I07",
        event_type=templates.APPROVAL_REQUIRED,
        entity_type="RECOMMENDATION",
        entity_id="REC-1",
        trigger_id="ledger-10",
        action_path="/inventory-planning/recommendations/REC-1?review=1",
        plant="1300",
        context={"material": "5000092261"},
    )
    fields.update(overrides)
    return NotificationEvent(**fields)


def rows(db):
    return list(db.scalars(select(NotificationRow).order_by(NotificationRow.channel)))


class FakeAdapter:
    def __init__(self, outcome=NotificationOutcome.SENT, detail="ok", raises=False):
        self.outcome, self.detail, self.raises, self.intents = outcome, detail, raises, []

    def send(self, intent):
        self.intents.append(intent)
        if self.raises:
            raise RuntimeError("boom")
        return NotificationResult(outcome=self.outcome, detail=self.detail)


def dispatch(db, adapter, *, now=NOW, retry_max=3, retry_interval=300):
    return service.dispatch_pending(db, adapter, base_url=BASE, retry_max=retry_max, retry_interval_seconds=retry_interval, now=now)


# --- enqueue -----------------------------------------------------------------


def test_a_user_with_an_email_gets_an_inbox_row_and_a_pending_email(db):
    service.enqueue(db, event(), [Recipient(user_oid=OID, email="asha@example.com", role="Spares.EngineeringHead", plant="1300")], now=NOW)
    db.commit()

    inbox, email = rows(db)[1], rows(db)[0]
    assert (email.channel, email.status) == ("EMAIL", "PENDING")
    assert (inbox.channel, inbox.status, inbox.is_read) == ("IN_APP", "SENT", False)
    assert inbox.recipient_user_id == OID
    assert inbox.title == "Approval Required – Material Recommendation"
    assert "Plant 1300" in inbox.description and "5000092261" in inbox.description
    assert inbox.module == "I07"  # internal bookkeeping only; the wording never shows it


def test_the_same_event_twice_adds_nothing(db):
    recipient = Recipient(user_oid=OID, email="asha@example.com")
    first = service.enqueue(db, event(), [recipient], now=NOW)
    db.commit()
    again = service.enqueue(db, event(), [recipient], now=NOW)
    db.commit()
    assert len(first) == 2 and again == [] and len(rows(db)) == 2


def test_a_different_trigger_is_a_new_notification(db):
    recipient = Recipient(user_oid=OID, email="asha@example.com")
    service.enqueue(db, event(trigger_id="ledger-10"), [recipient])
    service.enqueue(db, event(trigger_id="ledger-11"), [recipient])
    db.commit()
    assert len(rows(db)) == 4


def test_an_unresolved_recipient_is_recorded_as_a_failure_not_dropped(db):
    service.enqueue(db, event(), [Recipient(role="Spares.PlantHead", plant="1300", error="no recipient configured for role Spares.PlantHead at plant 1300")])
    db.commit()
    [row] = rows(db)
    assert row.status == "FAILED" and "Spares.PlantHead" in row.error
    assert row.recipient_email is None


def test_an_email_only_recipient_gets_no_inbox_row(db):
    service.enqueue(db, event(), [Recipient(email="ops@example.com")])
    db.commit()
    assert [r.channel for r in rows(db)] == ["EMAIL"]


# --- dispatch ----------------------------------------------------------------


def queued(db, **recipient):
    service.enqueue(db, event(), [Recipient(email="asha@example.com", **recipient)], now=NOW)
    db.commit()


def test_the_logging_adapter_never_produces_sent(db):
    queued(db)
    result = dispatch(db, LoggingNotificationAdapter())
    [row] = rows(db)
    assert row.status == "LOGGED_ONLY" and row.sent_at is None
    assert (result.sent, result.logged_only, result.failed) == (0, 1, 0)


def test_a_provider_that_accepts_the_message_is_sent_and_gets_the_link(db):
    queued(db)
    adapter = FakeAdapter()
    result = dispatch(db, adapter)
    [row] = rows(db)
    assert row.status == "SENT" and row.sent_at is not None and result.sent == 1
    [intent] = adapter.intents
    assert intent.action_url == f"{BASE}/inventory-planning/recommendations/REC-1?review=1"
    assert intent.action_label == "Open Review Workspace"
    assert intent.subject == "Approval Required – Material Recommendation"


def test_a_failed_email_is_retried_later_and_stops_at_the_limit(db):
    queued(db)
    adapter = FakeAdapter(outcome=NotificationOutcome.FAILED, detail="mailbox unavailable")

    dispatch(db, adapter, now=NOW)
    [row] = rows(db)
    assert (row.status, row.retry_count, row.error) == ("FAILED", 1, "mailbox unavailable")

    assert dispatch(db, adapter, now=NOW + timedelta(seconds=60)).failed == 0  # not due yet
    assert len(adapter.intents) == 1

    dispatch(db, adapter, now=NOW + timedelta(seconds=301))
    dispatch(db, adapter, now=NOW + timedelta(seconds=700))
    assert rows(db)[0].retry_count == 3 and rows(db)[0].next_attempt_at is None
    assert dispatch(db, adapter, now=NOW + timedelta(days=1)).failed == 0  # gave up
    assert len(adapter.intents) == 3


def test_an_adapter_that_raises_is_recorded_and_does_not_stop_the_rest(db):
    service.enqueue(db, event(entity_id="REC-1"), [Recipient(email="a@example.com")], now=NOW)
    service.enqueue(db, event(entity_id="REC-2"), [Recipient(email="b@example.com")], now=NOW)
    db.commit()

    class Flaky(FakeAdapter):
        def send(self, intent):
            if intent.recipient == "a@example.com":
                raise RuntimeError("boom")
            return super().send(intent)

    dispatch(db, Flaky())
    statuses = {r.recipient_email: (r.status, r.error) for r in rows(db)}
    assert statuses["a@example.com"][0] == "FAILED" and "boom" in statuses["a@example.com"][1]
    assert statuses["b@example.com"][0] == "SENT"


def test_a_row_stuck_in_processing_is_picked_up_again_after_the_lease(db):
    queued(db)
    row = rows(db)[0]
    row.status, row.next_attempt_at = "PROCESSING", NOW + timedelta(seconds=service.CLAIM_LEASE_SECONDS)
    db.commit()
    assert dispatch(db, FakeAdapter(), now=NOW + timedelta(seconds=60)).sent == 0
    assert dispatch(db, FakeAdapter(), now=NOW + timedelta(seconds=service.CLAIM_LEASE_SECONDS + 1)).sent == 1


# --- choosing the adapter ----------------------------------------------------


def test_logging_is_the_default_adapter():
    assert isinstance(service.get_email_adapter(get_settings().model_copy(update={"notification_email_provider": "logging"})), LoggingNotificationAdapter)


def test_graph_without_its_settings_fails_loudly_instead_of_pretending():
    settings = get_settings().model_copy(update={"notification_email_provider": "graph", "graph_tenant_id": "", "graph_client_id": "", "graph_client_secret": "", "graph_sender_mailbox": ""})
    adapter = service.get_email_adapter(settings)
    assert isinstance(adapter, UnconfiguredEmailAdapter)
    result = adapter.send(NotificationIntent("X", NotificationChannel.EMAIL, "a@example.com", "s", "b", action_url="https://x"))
    assert result.outcome is NotificationOutcome.FAILED and "GRAPH_TENANT_ID" in result.detail


# --- Graph adapter -----------------------------------------------------------


def graph(status_code=202, raises=False):
    calls = []

    def post(url, json, headers, timeout):
        calls.append(SimpleNamespace(url=url, json=json, headers=headers))
        if raises:
            raise ConnectionError("no network")
        return SimpleNamespace(status_code=status_code, text="denied")

    adapter = GraphEmailAdapter(tenant_id="t", client_id="c", client_secret="s", sender_mailbox="shared@example.com", post=post, token_provider=lambda: "TOKEN")
    return adapter, calls


def graph_intent(**overrides):
    fields = dict(
        exception_id="REC-1", channel=NotificationChannel.EMAIL, recipient="asha@example.com",
        subject="Approval Required – Material Recommendation", body="Material: 5000092261",
        action_url="https://spares.example.com/x?review=1", action_label="Open Review Workspace",
    )
    fields.update(overrides)
    return NotificationIntent(**fields)


def test_graph_accepted_is_the_only_sent():
    adapter, calls = graph(202)
    assert adapter.send(graph_intent()).outcome is NotificationOutcome.SENT
    [call] = calls
    assert call.url.endswith("/users/shared@example.com/sendMail")
    assert call.headers["Authorization"] == "Bearer TOKEN"
    message = call.json["message"]
    assert message["toRecipients"][0]["emailAddress"]["address"] == "asha@example.com"
    assert "https://spares.example.com/x?review=1" in message["body"]["content"]
    assert "Open Review Workspace" in message["body"]["content"]


def test_graph_refusal_and_network_errors_are_failures():
    refused = graph(403)[0].send(graph_intent())
    assert refused.outcome is NotificationOutcome.FAILED and "403" in refused.detail
    broken = graph(raises=True)[0].send(graph_intent())
    assert broken.outcome is NotificationOutcome.FAILED and "no network" in broken.detail


def test_graph_will_not_send_an_email_without_a_link_or_a_recipient():
    adapter, calls = graph()
    assert adapter.send(graph_intent(action_url=None)).outcome is NotificationOutcome.FAILED
    assert adapter.send(graph_intent(recipient=None)).outcome is NotificationOutcome.FAILED
    assert calls == []


def test_graph_escapes_html_in_the_body():
    adapter, calls = graph()
    adapter.send(graph_intent(body="<script>alert(1)</script>"))
    assert "<script>" not in calls[0].json["message"]["body"]["content"]


# --- I13's port --------------------------------------------------------------


def i13_intent(channel, recipient=OID):
    return NotificationIntent(
        exception_id="EX-1", channel=channel, recipient=recipient, subject="Action Required – Inventory Exception",
        body="Please confirm.", plant="1300", module="I13", event_type=templates.EXCEPTION_RESPONSE_REQUIRED,
        entity_type="EXCEPTION", action_path="/aging-exceptions",
    )


def test_i13_in_app_notification_lands_in_the_inbox_and_email_is_only_queued(db):
    db.add(AppUser(oid=OID, name="Requester", email="req@example.com"))
    db.commit()
    port = service.SharedNotificationPort(db)

    in_app = port.send(i13_intent(NotificationChannel.PLATFORM_QUEUE))
    email = port.send(i13_intent(NotificationChannel.EMAIL))
    db.commit()

    assert in_app.outcome is NotificationOutcome.SENT
    assert email.outcome is NotificationOutcome.LOGGED_ONLY  # queued, not delivered
    assert {(r.channel, r.status) for r in rows(db)} == {("IN_APP", "SENT"), ("EMAIL", "PENDING")}


def test_i13_with_no_recipient_is_a_failure(db):
    result = service.SharedNotificationPort(db).send(i13_intent(NotificationChannel.EMAIL, recipient=None))
    assert result.outcome is NotificationOutcome.FAILED


def test_i13_repeat_does_not_notify_twice(db):
    port = service.SharedNotificationPort(db)
    port.send(i13_intent(NotificationChannel.PLATFORM_QUEUE))
    port.send(i13_intent(NotificationChannel.PLATFORM_QUEUE))
    db.commit()
    assert len(rows(db)) == 1

"""H1 (the actor is the authenticated caller) and M3 (a missing email is never silent).

H1: the approval routes take the actor from the request's identity, never from the body, and an
identity is never turned into an email address to send to.
M3: a direct recipient with no usable email becomes a recorded failure, with the reason.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import USER_ROLES, CurrentUser
from app.integrations.notifications import service
from app.integrations.notifications.models import (
    Channel,
    NotificationChannel,
    NotificationEvent,
    NotificationIntent,
    NotificationOutcome,
    Recipient,
)
from app.integrations.notifications.resolver import (
    INVALID_IDENTITY,
    MISSING_IDENTITY,
    NO_EMAIL,
    UNKNOWN_USER,
    is_entra_object_id,
    resolve_role,
    resolve_user,
)
from app.models.app_user import AppUser
from app.models.i7_recommendation import ApprovalLedgerEntry
from app.models.notification import NotificationRecipient, NotificationRow
from tests.notifications.test_i07_approvals import BASE, SUBMITTER, act, configure, notes, person, recommendation, submit
from app.initiatives.i7.recommendations.types import ApprovalRole

OID = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
VICTIM = "victim@example.com"


def all_rows(db):
    return list(db.scalars(select(NotificationRow)))


# --- H1 ----------------------------------------------------------------------


def test_the_ledger_records_the_authenticated_caller_not_the_body(db, api):
    configure(db)
    rec = recommendation(db)

    api.post(f"{BASE}/{rec}/submit", json={"actor_id": OTHER}, headers={"X-Actor-Id": SUBMITTER})
    api.post(
        f"{BASE}/{rec}/actions",
        json={"actor_id": OTHER, "actor_role": "End User", "action": "APPROVE"},
        headers={"X-Actor-Id": person(ApprovalRole.END_USER)},
    )

    actors = [e.actor_id for e in db.scalars(select(ApprovalLedgerEntry).order_by(ApprovalLedgerEntry.id))]
    assert actors == [SUBMITTER, person(ApprovalRole.END_USER)]
    assert OTHER not in actors


def test_an_email_address_in_the_body_cannot_receive_a_notification(db, api):
    """The attack: submit with someone else's email as actor_id, then act so a notice goes to 'the submitter'."""
    configure(db)
    rec = recommendation(db)

    api.post(f"{BASE}/{rec}/submit", json={"actor_id": VICTIM}, headers={"X-Actor-Id": SUBMITTER})
    act(api, rec, ApprovalRole.END_USER, "HOLD", comment="Waiting for a quote, see http://phish.example.com")
    act(api, rec, ApprovalRole.END_USER, "REJECT", comment="No longer needed")

    rows = all_rows(db)
    assert rows, "the real submitter and approvers should still be notified"
    assert VICTIM not in {r.recipient_email for r in rows}
    assert VICTIM not in {r.recipient_user_id for r in rows}
    assert VICTIM not in " ".join((r.error or "") + r.title + r.description for r in rows).replace("http://phish.example.com", "")
    # the submitter notices went to the real caller
    assert SUBMITTER in {r.recipient_user_id for r in rows if r.event_type in ("APPROVAL_ON_HOLD", "APPROVAL_REJECTED")}


def test_an_email_address_as_the_caller_identity_is_refused_as_invalid_not_emailed(db, api):
    """With sign-in off the identity is the header. An email there must not become a recipient either."""
    configure(db)
    rec = recommendation(db)

    api.post(f"{BASE}/{rec}/submit", json={}, headers={"X-Actor-Id": VICTIM})
    act(api, rec, ApprovalRole.END_USER, "HOLD", comment="Waiting")

    rows = all_rows(db)
    assert VICTIM not in {r.recipient_email for r in rows}
    failed = [r for r in rows if r.event_type == "APPROVAL_ON_HOLD"]
    assert failed and all(r.status == "FAILED" and INVALID_IDENTITY in r.error for r in failed)


def test_the_acting_user_is_excluded_by_their_real_identity(db, api):
    configure(db)
    rec = recommendation(db)
    api.post(f"{BASE}/{rec}/submit", json={}, headers={"X-Actor-Id": person(ApprovalRole.END_USER)})  # the End User submits
    act(api, rec, ApprovalRole.END_USER, "HOLD", comment="Waiting")

    # the person who held it (and also submitted) is not told about their own action
    assert [n for n in notes(db, "APPROVAL_ON_HOLD")] == []


def test_with_sign_in_on_the_actor_is_the_token_user_even_if_the_header_says_otherwise(db, api, monkeypatch):
    configure(db)
    rec = recommendation(db)
    monkeypatch.setattr(get_settings(), "auth_enabled", True)
    monkeypatch.setattr(
        "app.core.security.authenticate", lambda request: CurrentUser(oid=SUBMITTER, name="S", email="s@example.com", roles=USER_ROLES)
    )

    response = api.post(f"{BASE}/{rec}/submit", json={"actor_id": VICTIM}, headers={"X-Actor-Id": OTHER})

    assert response.status_code == 200, response.text
    [entry] = db.scalars(select(ApprovalLedgerEntry)).all()
    assert entry.actor_id == SUBMITTER


def test_old_clients_that_still_send_actor_id_are_not_rejected(db, api):
    configure(db)
    rec = recommendation(db)
    assert api.post(f"{BASE}/{rec}/submit", json={"actor_id": "end-user"}, headers={"X-Actor-Id": SUBMITTER}).status_code == 200
    assert api.post(f"{BASE}/{rec}/submit", json={}, headers={"X-Actor-Id": SUBMITTER}).status_code in (200, 409)


@pytest.mark.parametrize(
    "value, expected",
    [
        (OID, True),
        (OID.upper(), True),
        ("end-user", False),
        ("victim@example.com", False),
        ("11111111-1111-1111-1111-11111111111", False),  # one character short
        ("11111111111111111111111111111111", False),  # no dashes
        ("11111111-1111-1111-1111-1111111111111", False),  # one too long
        ("", False),
        (None, False),
    ],
)
def test_object_id_format(value, expected):
    assert is_entra_object_id(value) is expected


# --- M3 ----------------------------------------------------------------------


def test_a_valid_oid_with_an_email_resolves_to_that_email(db):
    db.add(AppUser(oid=OID, name="Asha", email="asha@example.com"))
    [r] = resolve_user(db, OID)
    assert (r.user_oid, r.email, r.error, r.email_error) == (OID, "asha@example.com", None, None)


def test_a_valid_oid_without_an_email_is_a_recorded_failure_not_a_silent_skip(db):
    db.add(AppUser(oid=OID, name="Asha", email=""))
    [r] = resolve_user(db, OID)
    assert r.user_oid == OID and r.email is None and r.email_error == NO_EMAIL

    service.enqueue(
        db,
        NotificationEvent(module="I07", event_type="APPROVAL_ON_HOLD", entity_type="RECOMMENDATION", entity_id="R1", trigger_id="1"),
        [r],
    )
    db.commit()
    rows = {(row.channel, row.status): row for row in all_rows(db)}
    assert rows[("IN_APP", "SENT")].recipient_user_id == OID  # the inbox still works
    assert rows[("EMAIL", "FAILED")].error == NO_EMAIL


def test_a_user_who_has_never_signed_in_is_a_different_recorded_failure(db):
    [r] = resolve_user(db, OID)
    assert r.user_oid == OID and r.email is None and r.email_error == UNKNOWN_USER
    assert UNKNOWN_USER != NO_EMAIL


@pytest.mark.parametrize("identity", ["end-user", "victim@example.com", "not-a-guid", "11111111-1111-1111-1111-11111111111"])
def test_an_invalid_identity_is_refused_and_never_stored(db, identity):
    [r] = resolve_user(db, identity)
    assert r.error == INVALID_IDENTITY and r.user_oid is None and r.email is None

    service.enqueue(
        db, NotificationEvent(module="I07", event_type="APPROVAL_ON_HOLD", entity_type="RECOMMENDATION", entity_id="R1", trigger_id="1"), [r]
    )
    db.commit()
    [row] = all_rows(db)
    assert row.status == "FAILED" and row.error == INVALID_IDENTITY
    assert row.recipient_user_id is None and row.recipient_email is None
    assert identity not in (row.error + row.title + row.description)


@pytest.mark.parametrize("identity", [None, "", "   "])
def test_a_missing_identity_is_its_own_failure(db, identity):
    [r] = resolve_user(db, identity)
    assert r.error == MISSING_IDENTITY


def test_the_configured_fallback_email_is_used_when_a_known_user_has_no_email(db):
    db.add(AppUser(oid=OID, name="Asha", email=""))
    [r] = resolve_user(db, OID, fallback_email="ops@example.com")
    assert (r.user_oid, r.email, r.email_error) == (OID, "ops@example.com", None)

    service.enqueue(
        db, NotificationEvent(module="I07", event_type="APPROVAL_ON_HOLD", entity_type="RECOMMENDATION", entity_id="R1", trigger_id="1"), [r]
    )
    db.commit()
    assert {(row.channel, row.status) for row in all_rows(db)} == {("IN_APP", "SENT"), ("EMAIL", "PENDING")}


def test_the_fallback_also_covers_a_user_who_has_never_signed_in(db):
    [r] = resolve_user(db, OID, fallback_email="ops@example.com")
    assert r.email == "ops@example.com" and r.email_error is None


def test_without_a_fallback_the_failure_names_the_reason(db):
    [r] = resolve_user(db, OID, fallback_email="")
    assert r.email is None and r.email_error


def test_the_fallback_is_never_used_for_an_invalid_identity(db):
    [r] = resolve_user(db, "victim@example.com", fallback_email="ops@example.com")
    assert r.email is None and r.error == INVALID_IDENTITY


def test_a_role_holder_with_no_email_is_recorded_too_and_can_use_the_fallback(db):
    db.add(NotificationRecipient(role="Spares.PlantHead", plant="1300", user_oid=OID, active=True))
    db.commit()
    [r] = resolve_role(db, "Spares.PlantHead", "1300")
    assert r.user_oid == OID and r.email_error == UNKNOWN_USER
    [with_fallback] = resolve_role(db, "Spares.PlantHead", "1300", fallback_email="ops@example.com")
    assert with_fallback.email == "ops@example.com"


def test_a_recipient_row_with_a_bad_identity_is_reported_not_used(db):
    db.add(NotificationRecipient(role="Spares.PlantHead", plant="1300", user_oid="end-user", active=True))
    db.commit()
    [r] = resolve_role(db, "Spares.PlantHead", "1300")
    assert r.error and INVALID_IDENTITY in r.error


def test_i13s_port_reports_a_missing_email_as_failed_not_queued(db):
    db.add(AppUser(oid=OID, name="Asha", email=""))
    db.commit()
    intent = NotificationIntent(
        exception_id="EX-1", channel=NotificationChannel.EMAIL, recipient=OID, subject="s", body="b", module="I13", plant="1300"
    )

    result = service.SharedNotificationPort(db).send(intent)
    db.commit()

    assert result.outcome is NotificationOutcome.FAILED and result.detail == NO_EMAIL
    assert [(r.channel, r.status) for r in all_rows(db)] == [("EMAIL", "FAILED")]


def test_i13s_port_refuses_an_email_address_as_the_recipient(db):
    intent = NotificationIntent(exception_id="EX-1", channel=NotificationChannel.EMAIL, recipient=VICTIM, subject="s", body="b", module="I13")
    result = service.SharedNotificationPort(db).send(intent)
    db.commit()
    assert result.outcome is NotificationOutcome.FAILED and INVALID_IDENTITY in result.detail
    assert VICTIM not in {r.recipient_email for r in all_rows(db)}

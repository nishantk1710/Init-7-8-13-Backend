"""A purchase requisition from SAP: everyone is told it arrived, only administrators see what it is."""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from app.integrations.notifications import templates
from app.models.app_user import AppUser
from app.models.notification import NotificationRecipient, NotificationRow
from app.services import pr_event_notifications as pr_notes

ADMIN = "aaaaaaaa-0000-4000-8000-000000000001"
USER_1 = "bbbbbbbb-0000-4000-8000-000000000002"
USER_2 = "cccccccc-0000-4000-8000-000000000003"
EVENT = {
    "BANFN": "1000000567",
    "CREATED_ON": "20260916",
    "CREATED_BY": "VSUNEEL",
    "CREATED_AT": "131403",
    "MESSAGE": "PR Created Successfully",
}
RECEIVED = "2026-11-03T09:00:00+00:00"


def _people(db, *, admin=True):
    for oid in (ADMIN, USER_1, USER_2):
        db.add(AppUser(oid=oid, name=oid[:8], email=f"{oid[:8]}@example.com"))
    db.add(AppUser(oid=USER_1, name="renamed", email="new@example.com"))  # a second row for one person
    db.add(AppUser(oid="UNAUTHENTICATED-FRONTEND", name="", email=""))  # a placeholder, never notified
    if admin:
        db.add(NotificationRecipient(role="Spares.Admin", plant="1300", user_oid=ADMIN, active=True))
    db.commit()


def _rows(db):
    return list(db.scalars(select(NotificationRow).order_by(NotificationRow.id)))


def test_everyone_signed_in_is_told_and_nothing_about_the_pr_reaches_them(db):
    _people(db)
    counts = pr_notes.notify_pr_received(db, EVENT, received_utc=RECEIVED)
    db.commit()

    plain = [r for r in _rows(db) if r.event_type == templates.PR_RECEIVED]
    assert sorted(r.recipient_user_id for r in plain) == [USER_1, USER_2]
    assert counts == {"users": 2, "admins": 1, "written": 3}
    for row in plain:
        assert row.channel == "IN_APP" and row.status == "SENT"
        assert row.entity_id == ""  # the inbox page shows it as "Ref ..."; empty hides it
        assert row.title == "New Purchase Requisition Received"
        assert row.description == "A new purchase requisition was received from SAP."
        for value in ("1000000567", "VSUNEEL", "PR Created Successfully"):
            for column in (row.title, row.description, row.entity_id, row.action_path or "", row.idempotency_key):
                assert value not in column, (value, column)


def test_administrators_get_the_details_instead_of_the_plain_notice(db):
    _people(db)
    pr_notes.notify_pr_received(db, EVENT, received_utc=RECEIVED)
    db.commit()

    mine = [r for r in _rows(db) if r.recipient_user_id == ADMIN]
    assert [r.event_type for r in mine] == [templates.PR_RECEIVED_DETAILS]
    row = mine[0]
    assert row.title == "Purchase Requisition 1000000567 Received"
    assert "Created on: 2026-09-16 at 13:14:03" in row.description
    assert "Created by (SAP user): VSUNEEL" in row.description
    assert "SAP message: PR Created Successfully" in row.description
    assert row.entity_id == "1000000567"


def test_the_same_event_twice_adds_nothing(db):
    _people(db)
    pr_notes.notify_pr_received(db, EVENT, received_utc=RECEIVED)
    db.commit()
    again = pr_notes.notify_pr_received(db, EVENT, received_utc="2026-11-03T09:05:00+00:00")
    db.commit()
    assert again["written"] == 0
    assert len(_rows(db)) == 3


def test_a_new_requisition_is_a_new_notice(db):
    _people(db)
    pr_notes.notify_pr_received(db, EVENT, received_utc=RECEIVED)
    pr_notes.notify_pr_received(db, {**EVENT, "BANFN": "1000000568"}, received_utc=RECEIVED)
    db.commit()
    assert len(_rows(db)) == 6


def test_no_administrator_is_a_visible_failure_and_users_are_still_told(db):
    _people(db, admin=False)
    pr_notes.notify_pr_received(db, EVENT, received_utc=RECEIVED)
    db.commit()

    rows = _rows(db)
    assert sorted(r.recipient_user_id for r in rows if r.event_type == templates.PR_RECEIVED) == [ADMIN, USER_1, USER_2]
    failed = [r for r in rows if r.status == "FAILED"]
    assert len(failed) == 1
    assert failed[0].channel == "IN_APP" and failed[0].recipient_user_id is None
    assert "no recipient configured for role Spares.Admin" in failed[0].error


def test_in_app_only_even_with_email_switched_on(db, monkeypatch):
    from app.integrations.notifications import service

    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(notification_email_enabled=True))
    _people(db)
    pr_notes.notify_pr_received(db, EVENT, received_utc=RECEIVED)
    db.commit()
    assert {r.channel for r in _rows(db)} == {"IN_APP"}


def test_inactive_and_identity_less_admin_rows_are_ignored(db):
    _people(db, admin=False)
    db.add(NotificationRecipient(role="Spares.Admin", plant=None, user_oid=ADMIN, active=False))
    db.add(NotificationRecipient(role="Spares.Admin", plant=None, user_oid=None, email="ops@example.com", active=True))
    db.commit()
    assert pr_notes.administrators(db) == []


def test_an_event_without_a_banfn_is_still_announced(db):
    _people(db)
    pr_notes.notify_pr_received(db, {"MESSAGE": "PR Created Successfully"}, received_utc=RECEIVED)
    db.commit()
    plain = [r for r in _rows(db) if r.event_type == templates.PR_RECEIVED]
    assert len(plain) == 2
    assert all(r.entity_id == "" for r in _rows(db))


def test_lower_case_keys_are_read_too(db):
    _people(db)
    pr_notes.notify_pr_received(db, {k.lower(): v for k, v in EVENT.items()}, received_utc=RECEIVED)
    db.commit()
    details = [r for r in _rows(db) if r.event_type == templates.PR_RECEIVED_DETAILS]
    assert details[0].title == "Purchase Requisition 1000000567 Received"


def test_a_plain_user_sees_no_pr_detail_through_the_inbox_api(db, api):
    _people(db)
    pr_notes.notify_pr_received(db, EVENT, received_utc=RECEIVED)
    db.commit()

    body = api.get("/api/notifications", headers={"X-Actor-Id": USER_1}).json()
    assert body["total"] == 1
    assert "1000000567" not in str(body) and "VSUNEEL" not in str(body)

    admin = api.get("/api/notifications", headers={"X-Actor-Id": ADMIN}).json()
    assert admin["items"][0]["title"] == "Purchase Requisition 1000000567 Received"


def test_the_background_task_does_nothing_without_a_database(monkeypatch):
    called = []
    monkeypatch.setattr(pr_notes, "get_settings", lambda: SimpleNamespace(pr_event_notifications_enabled=True, database_url=""))
    monkeypatch.setattr(pr_notes, "notify_pr_received", lambda *a, **k: called.append(a))
    pr_notes.notify_in_background(EVENT, RECEIVED)
    assert called == []


def test_the_background_task_can_be_switched_off(monkeypatch):
    called = []
    monkeypatch.setattr(pr_notes, "get_settings", lambda: SimpleNamespace(pr_event_notifications_enabled=False, database_url="x"))
    monkeypatch.setattr(pr_notes, "notify_pr_received", lambda *a, **k: called.append(a))
    pr_notes.notify_in_background(EVENT, RECEIVED)
    assert called == []


def test_the_background_task_never_raises(monkeypatch):
    monkeypatch.setattr(pr_notes, "get_settings", lambda: SimpleNamespace(pr_event_notifications_enabled=True, database_url="x"))

    def broken():
        raise RuntimeError("database unreachable")

    import app.core.db as core_db

    monkeypatch.setattr(core_db, "get_sessionmaker", broken)
    pr_notes.notify_in_background(EVENT, RECEIVED)  # logged, not raised


def test_the_endpoint_schedules_the_notice_after_its_202(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    seen = []
    monkeypatch.setattr(pr_notes, "notify_in_background", lambda payload, received: seen.append((payload, received)))
    response = TestClient(app).post("/api/events/pr", json=EVENT)
    assert response.status_code == 202
    assert len(seen) == 1 and seen[0][0]["BANFN"] == "1000000567"

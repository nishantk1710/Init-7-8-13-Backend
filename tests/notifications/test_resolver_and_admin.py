"""Recipient resolution and the administrator endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import get_db
from app.integrations.notifications.resolver import resolve_role, resolve_user
from app.main import create_app
from app.models.app_user import AppUser
from app.models.notification import NotificationConfigAudit, NotificationRecipient

OID_A = "11111111-1111-1111-1111-111111111111"
OID_B = "22222222-2222-2222-2222-222222222222"


def add(db, **fields):
    row = NotificationRecipient(active=True, **fields)
    db.add(row)
    db.commit()
    return row


def test_role_and_plant_pick_the_right_person_and_their_email_comes_from_app_user(db):
    db.add(AppUser(oid=OID_A, name="Asha", email="asha@example.com"))
    add(db, role="Spares.EngineeringHead", plant="1300", user_oid=OID_A)
    add(db, role="Spares.EngineeringHead", plant="1500", user_oid=OID_B)

    [found] = resolve_role(db, "Spares.EngineeringHead", "1300")

    assert (found.user_oid, found.email, found.error) == (OID_A, "asha@example.com", None)


def test_a_row_without_a_plant_applies_to_every_plant(db):
    add(db, role="Spares.PlantHead", plant=None, user_oid=OID_A)
    assert resolve_role(db, "Spares.PlantHead", "1500")[0].user_oid == OID_A


def test_several_people_can_hold_a_role(db):
    add(db, role="Spares.CommercialHead", plant="1300", user_oid=OID_A)
    add(db, role="Spares.CommercialHead", plant="1300", email="second@example.com")
    assert len(resolve_role(db, "Spares.CommercialHead", "1300")) == 2


def test_the_fallback_email_on_the_row_is_used_when_the_user_has_never_signed_in(db):
    add(db, role="Spares.PlantHead", plant="1300", user_oid=OID_A, email="fallback@example.com")
    assert resolve_role(db, "Spares.PlantHead", "1300")[0].email == "fallback@example.com"


def test_inactive_rows_are_ignored(db):
    row = add(db, role="Spares.PlantHead", plant="1300", user_oid=OID_A)
    row.active = False
    db.commit()
    assert resolve_role(db, "Spares.PlantHead", "1300")[0].error


def test_unknown_role_is_a_clear_error_not_a_guess(db):
    [found] = resolve_role(db, "Spares.PlantHead", "1300")
    assert found.error and "Spares.PlantHead" in found.error and "1300" in found.error
    assert found.email is None and found.user_oid is None


def test_configured_fallback_is_used_only_when_nobody_is_configured(db):
    [found] = resolve_role(db, "Spares.PlantHead", "1300", fallback_email="ops@example.com")
    assert found.email == "ops@example.com" and found.error is None


def test_a_row_with_neither_user_nor_email_is_reported(db):
    add(db, role="Spares.PlantHead", plant="1300")
    assert resolve_role(db, "Spares.PlantHead", "1300")[0].error


@pytest.fixture
def client(db):
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)


def test_admin_can_add_and_change_a_recipient_and_each_change_is_audited(client, db):
    created = client.post(
        "/api/notifications/admin/recipients",
        json={"role": "Spares.EngineeringHead", "plant": "1300", "user_oid": OID_A},
        headers={"X-Actor-Id": "admin-1"},
    )
    assert created.status_code == 201, created.text
    recipient_id = created.json()["id"]

    changed = client.post(
        f"/api/notifications/admin/recipients/{recipient_id}/update",
        json={"role": "Spares.EngineeringHead", "plant": "1300", "user_oid": OID_B, "active": False},
        headers={"X-Actor-Id": "admin-1"},
    )
    assert changed.status_code == 200 and changed.json()["user_oid"] == OID_B

    # retired, not deleted: the row and its history stay
    [row] = client.get("/api/notifications/admin/recipients").json()
    assert row["active"] is False

    actions = [a.action for a in db.scalars(select(NotificationConfigAudit).order_by(NotificationConfigAudit.id))]
    assert actions == ["CREATE", "UPDATE"]


def test_admin_rejects_an_unknown_role_and_a_recipient_with_nobody_in_it(client):
    bad_role = client.post("/api/notifications/admin/recipients", json={"role": "Spares.Nope", "user_oid": OID_A})
    nobody = client.post("/api/notifications/admin/recipients", json={"role": "Spares.PlantHead", "plant": "1300"})
    assert bad_role.status_code == 422 and nobody.status_code == 422


SLA = {"approval_route": "ROP_MAX", "first_reminder_minutes": 60, "repeat_minutes": 120, "max_reminders": 2}


def test_admin_can_set_and_switch_off_sla_timings_and_each_change_is_audited(client, db):
    created = client.post("/api/notifications/admin/sla", json={**SLA, "escalate_after_minutes": 240, "escalation_role": "Spares.PlantHead"})
    assert created.status_code == 201, created.text
    sla_id = created.json()["id"]

    off = client.post(f"/api/notifications/admin/sla/{sla_id}/update", json={**SLA, "active": False})
    assert off.status_code == 200 and off.json()["active"] is False

    actions = [a.action for a in db.scalars(select(NotificationConfigAudit).order_by(NotificationConfigAudit.id))]
    assert actions == ["CREATE", "UPDATE"]


def test_sla_validation(client):
    assert client.post("/api/notifications/admin/sla", json={**SLA, "approval_route": "SOMETHING"}).status_code == 422
    assert client.post("/api/notifications/admin/sla", json={**SLA, "first_reminder_minutes": 0}).status_code == 422
    assert client.post("/api/notifications/admin/sla", json={**SLA, "escalate_after_minutes": 60}).status_code == 422  # needs a role too
    assert client.post("/api/notifications/admin/sla", json={**SLA, "role": "Spares.Nope"}).status_code == 422


def test_only_one_sla_per_route_and_role(client):
    assert client.post("/api/notifications/admin/sla", json=SLA).status_code == 201
    assert client.post("/api/notifications/admin/sla", json=SLA).status_code == 409


def test_nothing_in_the_notification_api_uses_put_or_delete(client):
    paths = client.get("/openapi.json").json()["paths"]
    verbs = {verb for path, ops in paths.items() if path.startswith("/api/notifications") for verb in ops}
    assert verbs <= {"get", "post"}

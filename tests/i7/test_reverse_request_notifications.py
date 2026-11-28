"""Min-Max -> OAR conversion requests notify the approvers, end to end through the real routes.

The same people are told at the same moments as for an OAR -> Min-Max approval: the first approver when a request is
raised, the next one after each approval, the requester when it ends. Only the database is in-memory SQLite.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from reverse_conversion_support import FakeSource, population
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.initiatives.i7.notifications import APPROVAL_ROLE_TO_ENTRA
from app.initiatives.i7.recommendations.types import OAR_APPROVAL_CHAIN, ApprovalRole
from app.initiatives.i7.reverse_approval import notifications as reverse_notifications
from app.initiatives.i7.reverse_conversion import snapshot
from app.initiatives.i7.reverse_conversion.snapshot import run_reverse_conversion
from app.models.app_user import AppUser
from app.models.base import Base
from app.models.i7_reverse import ReverseApprovalLedger, ReverseConversionRequest, ReverseRecommendation, ReverseRun
from app.models.notification import NotificationRecipient, NotificationRow

from tests.notifications.conftest import api, db  # noqa: F401  (fixtures)

REQUESTS = "/api/v1/i7/reverse-conversion/requests"
REVERSE_TABLES = [ReverseRun.__table__, ReverseRecommendation.__table__, ReverseConversionRequest.__table__, ReverseApprovalLedger.__table__]
PEOPLE = {role: f"{index:08d}-0000-0000-0000-000000000000" for index, role in enumerate(ApprovalRole)}
REQUESTER = "99999999-0000-0000-0000-000000000000"


@pytest.fixture
def configured(db, monkeypatch):  # noqa: F811
    """A stored Min-Max to OAR run, and one person per approval role at plant 1300 (plus the requester)."""
    monkeypatch.setattr(
        snapshot, "get_settings", lambda: SimpleNamespace(i7_reverse_anchor_month="2026-10", i7_reverse_anchor_source="configured", i7_rationale_concurrency=2)
    )
    engine = db.get_bind()
    Base.metadata.create_all(engine, tables=REVERSE_TABLES)
    run_reverse_conversion(session_factory=sessionmaker(bind=engine), source_factory=lambda s: FakeSource(*population()))
    for role in ApprovalRole:
        oid = PEOPLE[role]
        db.add(AppUser(oid=oid, name=role.value, email=f"{oid[:8]}@example.com"))
        db.add(NotificationRecipient(role=APPROVAL_ROLE_TO_ENTRA[role], plant="1300", user_oid=oid, active=True))
    db.add(AppUser(oid=REQUESTER, name="Requester", email="requester@example.com"))
    db.commit()
    return db


def raise_request(api, material="REC1"):  # noqa: F811
    response = api.post(REQUESTS, json={"material": material, "plant": "1300"}, headers={"X-Actor-Id": REQUESTER})
    assert response.status_code == 201, response.text
    return response.json()


def act(api, request_id, role, action, comment=None):  # noqa: F811
    return api.post(
        f"{REQUESTS}/{request_id}/actions",
        json={"actor_role": role.value, "action": action, "comment": comment},
        headers={"X-Actor-Id": PEOPLE[role]},
    )


def notes(db, event_type, channel="IN_APP"):  # noqa: F811
    return list(
        db.scalars(
            select(NotificationRow)
            .where(NotificationRow.event_type == event_type, NotificationRow.channel == channel)
            .order_by(NotificationRow.idempotency_key)
        )
    )


def test_raising_a_request_tells_the_first_approver_and_links_to_the_decision_page(configured, api):  # noqa: F811
    request = raise_request(api)

    [inbox] = notes(configured, "APPROVAL_REQUIRED")

    assert inbox.recipient_user_id == PEOPLE[ApprovalRole.INVENTORY_CONTROLLER]
    assert inbox.title == "Approval Required – Convert to OAR"
    assert inbox.entity_type == "REVERSE_REQUEST" and inbox.entity_id == str(request["id"])
    assert inbox.action_path == "/inventory-planning/convert-to-oar?material=REC1&plant=1300"
    assert inbox.plant == "1300" and "REC1" in inbox.description
    [email] = notes(configured, "APPROVAL_REQUIRED", channel="EMAIL")
    assert email.status == "PENDING" and email.recipient_email


def test_each_approval_tells_the_next_role_in_the_chain(configured, api):  # noqa: F811
    request = raise_request(api)

    for role in OAR_APPROVAL_CHAIN[:-1]:
        assert act(api, request["id"], role, "APPROVE").status_code == 200

    assert [n.recipient_user_id for n in notes(configured, "APPROVAL_REQUIRED")] == [PEOPLE[r] for r in OAR_APPROVAL_CHAIN]


def test_the_last_approval_tells_the_requester_it_is_approved(configured, api):  # noqa: F811
    request = raise_request(api)
    for role in OAR_APPROVAL_CHAIN:
        act(api, request["id"], role, "APPROVE")

    [done] = notes(configured, "APPROVAL_COMPLETED")

    assert done.recipient_user_id == REQUESTER
    assert done.title == "Convert to OAR Approved – Material REC1"
    # Nobody is asked to decide any more, so it links to the Stock item to OAR tab, not the decision page.
    assert done.action_path == "/inventory-planning/recommendations?type=min_max_to_oar&material=REC1&plant=1300"


def test_a_rejection_tells_the_requester_with_the_comment_and_not_the_rejecter(configured, api):  # noqa: F811
    request = raise_request(api)

    assert act(api, request["id"], ApprovalRole.INVENTORY_CONTROLLER, "REJECT", comment="Still ordered monthly").status_code == 200

    [rejected] = notes(configured, "APPROVAL_REJECTED")
    assert rejected.recipient_user_id == REQUESTER
    assert "Still ordered monthly" in rejected.description


def test_declining_before_approval_notifies_nobody(configured, api):  # noqa: F811
    response = api.post(
        f"{REQUESTS}/decline", json={"material": "REC1", "plant": "1300", "comment": "Not adopting"}, headers={"X-Actor-Id": REQUESTER}
    )
    assert response.status_code == 201, response.text

    assert notes(configured, "APPROVAL_REJECTED") == []  # the only person who could be told is the one who declined


def test_a_notification_that_cannot_be_queued_never_blocks_the_request(configured, api, monkeypatch):  # noqa: F811
    def boom(*_args, **_kwargs):
        raise RuntimeError("mail system down")

    monkeypatch.setattr(reverse_notifications, "enqueue", boom)

    request = raise_request(api)

    assert request["status"] == "PENDING_APPROVAL"
    assert notes(configured, "APPROVAL_REQUIRED") == []


def test_the_list_route_returns_the_queue_with_counts(configured, api):  # noqa: F811
    request = raise_request(api)

    body = api.get(f"{REQUESTS}/list", params={"status": "PENDING_APPROVAL"}).json()

    assert body["total"] == 1 and body["counts"] == [{"status": "PENDING_APPROVAL", "count": 1}]
    [item] = body["items"]
    assert (item["id"], item["material"], item["plant"], item["pending_role"]) == (request["id"], "REC1", "1300", "Inventory Controller")
    assert item["route"][0] == "Inventory Controller" and item["description"]

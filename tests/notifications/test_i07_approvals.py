"""Approval notifications, end to end through the real approval routes.

Each test drives ``/submit`` and ``/actions`` and then looks at the
``notification`` table. The routes, workflow and ledger are the real ones; only
the database is in-memory SQLite.
"""

from __future__ import annotations

from sqlalchemy import select

from app.initiatives.i7.notifications import APPROVAL_ROLE_TO_ENTRA
from app.initiatives.i7.recommendations.types import APPROVAL_CHAIN, OAR_APPROVAL_CHAIN, ApprovalRole, LifecycleStatus
from app.models.app_user import AppUser
from app.models.i7_recommendation import Recommendation
from app.models.notification import NotificationRecipient, NotificationRow

BASE = "/api/v1/i7/recommendations"
ROLE_PEOPLE = {role: f"{index:08d}-0000-0000-0000-000000000000" for index, role in enumerate(list(APPROVAL_CHAIN) + list(OAR_APPROVAL_CHAIN))}
SUBMITTER = "99999999-0000-0000-0000-000000000000"


def person(role: ApprovalRole, plant: str = "1300") -> str:
    return ROLE_PEOPLE[role]


def configure(db, plant="1300"):
    """One person per approval role at ``plant`` (plus the submitter), all with an email."""
    for role in set(ApprovalRole):
        oid = person(role)
        db.add(AppUser(oid=oid, name=role.value, email=f"{oid[:8]}@example.com"))
        db.add(NotificationRecipient(role=APPROVAL_ROLE_TO_ENTRA[role], plant=plant, user_oid=oid, active=True))
    db.add(AppUser(oid=SUBMITTER, name="Submitter", email="submitter@example.com"))
    db.commit()


def recommendation(db, rec_id="REC-1", *, is_oar=False, plant="1300") -> str:
    db.add(
        Recommendation(
            recommendation_id=rec_id, sap_material_number="5000092261", sap_plant_code=plant, policy_id="p", policy_version=1,
            formula_version="f", status=LifecycleStatus.READY_FOR_REVIEW.value, impact_status="NOT_EVALUABLE_MISSING_RECOMMENDED",
            is_oar=is_oar,
        )
    )
    db.commit()
    return rec_id


def submit(api, rec_id):
    # The actor is the authenticated caller (here the X-Actor-Id header, as sign-in is off), never the body.
    response = api.post(f"{BASE}/{rec_id}/submit", json={}, headers={"X-Actor-Id": SUBMITTER})
    assert response.status_code == 200, response.text
    return response.json()


def act(api, rec_id, role, action, comment=None, actor=None):
    return api.post(
        f"{BASE}/{rec_id}/actions",
        json={"actor_role": role.value, "action": action, "comment": comment},
        headers={"X-Actor-Id": actor or person(role)},
    )


def notes(db, event_type=None, channel="IN_APP"):
    query = select(NotificationRow).where(NotificationRow.channel == channel).order_by(NotificationRow.idempotency_key)  # ids are random; the key ends in the ledger id order
    if event_type:
        query = query.where(NotificationRow.event_type == event_type)
    return list(db.scalars(query))


def recipients_of(db, event_type, channel="IN_APP"):
    return {n.recipient_user_id for n in notes(db, event_type, channel)}


# --- the two routes ----------------------------------------------------------


def test_submit_tells_the_first_approver_and_links_to_the_review_page(db, api):
    configure(db)
    submit(api, recommendation(db))

    [inbox] = notes(db, "APPROVAL_REQUIRED")
    assert inbox.recipient_user_id == person(ApprovalRole.END_USER)
    assert inbox.title == "Approval Required – Material Recommendation"
    assert inbox.action_path == "/inventory-planning/recommendations/REC-1?review=1"
    assert inbox.plant == "1300" and "5000092261" in inbox.description
    [email] = notes(db, "APPROVAL_REQUIRED", channel="EMAIL")
    assert email.status == "PENDING" and email.recipient_email


def test_approving_walks_the_rop_max_route_one_approver_at_a_time(db, api):
    configure(db)
    rec = recommendation(db)
    submit(api, rec)

    for role in APPROVAL_CHAIN[:-1]:
        assert act(api, rec, role, "APPROVE").status_code == 200

    notified = [n.recipient_user_id for n in notes(db, "APPROVAL_REQUIRED")]
    assert notified == [person(role) for role in APPROVAL_CHAIN]


def test_the_oar_route_notifies_its_own_roles_in_its_own_order(db, api):
    configure(db)
    rec = recommendation(db, "REC-OAR", is_oar=True)
    submit(api, rec)
    act(api, rec, OAR_APPROVAL_CHAIN[0], "APPROVE")
    act(api, rec, OAR_APPROVAL_CHAIN[1], "APPROVE")

    notified = [n.recipient_user_id for n in notes(db, "APPROVAL_REQUIRED")]
    assert notified == [person(role) for role in OAR_APPROVAL_CHAIN[:3]]


def test_the_approver_is_the_one_for_that_plant(db, api):
    configure(db, plant="1300")
    other = "55555555-0000-0000-0000-000000000000"
    db.add(NotificationRecipient(role=APPROVAL_ROLE_TO_ENTRA[ApprovalRole.END_USER], plant="1500", user_oid=other, active=True))
    db.commit()

    submit(api, recommendation(db, "REC-1500", plant="1500"))

    assert recipients_of(db, "APPROVAL_REQUIRED") == {other}


def test_final_approval_tells_the_submitter_only_when_no_sap_owner_is_configured(db, api):
    configure(db)
    rec = recommendation(db)
    submit(api, rec)
    for role in APPROVAL_CHAIN:
        act(api, rec, role, "APPROVE")

    [done] = notes(db, "APPROVAL_COMPLETED")
    assert done.recipient_user_id == SUBMITTER


def test_final_approval_also_tells_the_configured_sap_execution_role(db, api, monkeypatch):
    configure(db)
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "notification_sap_execution_role", "Spares.Stores")
    stores = "77777777-0000-0000-0000-000000000000"
    db.add(NotificationRecipient(role="Spares.Stores", plant="1300", user_oid=stores, active=True))
    db.commit()
    rec = recommendation(db)
    submit(api, rec)
    for role in APPROVAL_CHAIN:
        act(api, rec, role, "APPROVE")

    assert recipients_of(db, "APPROVAL_COMPLETED") == {SUBMITTER, stores}


def test_send_back_tells_the_previous_approver_and_the_submitter(db, api):
    configure(db)
    rec = recommendation(db)
    submit(api, rec)
    act(api, rec, ApprovalRole.END_USER, "APPROVE")
    act(api, rec, ApprovalRole.ENGINEERING_MANAGER, "SEND_BACK", comment="Check the lead time")

    assert recipients_of(db, "APPROVAL_SENT_BACK") == {person(ApprovalRole.END_USER), SUBMITTER}
    assert "Check the lead time" in notes(db, "APPROVAL_SENT_BACK")[0].description


def test_reject_tells_the_submitter_and_the_earlier_approvers_but_not_the_rejecter(db, api):
    configure(db)
    rec = recommendation(db)
    submit(api, rec)
    act(api, rec, ApprovalRole.END_USER, "APPROVE")
    act(api, rec, ApprovalRole.ENGINEERING_MANAGER, "REJECT", comment="Not justified")

    assert recipients_of(db, "APPROVAL_REJECTED") == {SUBMITTER, person(ApprovalRole.END_USER)}


def test_hold_tells_the_submitter_and_release_tells_the_submitter_and_the_pending_approver(db, api):
    configure(db)
    rec = recommendation(db)
    submit(api, rec)
    act(api, rec, ApprovalRole.END_USER, "HOLD", comment="Waiting for a quote")
    assert recipients_of(db, "APPROVAL_ON_HOLD") == {SUBMITTER}

    act(api, rec, ApprovalRole.END_USER, "RELEASE_HOLD", comment="Quote arrived")
    # the person who released it is not told about their own action
    assert recipients_of(db, "APPROVAL_HOLD_RELEASED") == {SUBMITTER}


def test_adjust_tells_the_pending_role_holder_but_not_the_adjuster(db, api):
    configure(db)
    colleague = "66666666-0000-0000-0000-000000000000"
    db.add(NotificationRecipient(role=APPROVAL_ROLE_TO_ENTRA[ApprovalRole.END_USER], plant="1300", user_oid=colleague, active=True))
    db.commit()
    rec = recommendation(db)
    submit(api, rec)
    act(api, rec, ApprovalRole.END_USER, "ADJUST", comment="Lower the maximum")

    assert recipients_of(db, "APPROVAL_ADJUSTED") == {colleague}


def test_a_rejected_step_sends_nothing(db, api):
    configure(db)
    rec = recommendation(db)
    submit(api, rec)
    before = len(notes(db))

    wrong_role = act(api, rec, ApprovalRole.WAREHOUSE_SUPERVISOR, "APPROVE")  # not their turn
    no_comment = act(api, rec, ApprovalRole.END_USER, "REJECT")  # a comment is required

    assert wrong_role.status_code == 409 and no_comment.status_code == 409
    assert len(notes(db)) == before


def test_an_approver_nobody_configured_is_recorded_as_a_failure_and_the_step_still_succeeds(db, api):
    rec = recommendation(db)  # no recipients configured at all

    submit(api, rec)

    [failure] = notes(db, "APPROVAL_REQUIRED", channel="EMAIL")
    assert failure.status == "FAILED" and "Spares.EndUser" in failure.error


def test_a_notification_that_cannot_be_built_does_not_stop_the_approval(db, api, monkeypatch):
    configure(db)
    monkeypatch.setattr("app.initiatives.i7.notifications.enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    assert api.post(f"{BASE}/{recommendation(db)}/submit", json={}, headers={"X-Actor-Id": SUBMITTER}).status_code == 200
    assert db.scalar(select(Recommendation.status)) == "PENDING_APPROVAL"

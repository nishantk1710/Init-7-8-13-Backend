"""The inbox API: only my notifications, and nothing of anyone else's."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models.notification import NotificationRow

ME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
BASE = "/api/notifications"


def as_user(user: str) -> dict:
    return {"X-Actor-Id": user}


def add(db, user, n=1, *, channel="IN_APP", is_read=False, minutes_ago=0, key=None) -> NotificationRow:
    row = NotificationRow(
        idempotency_key=key or f"k-{user[:4]}-{channel}-{n}-{minutes_ago}",
        module="I07", event_type="APPROVAL_REQUIRED", channel=channel, recipient_user_id=user, plant="1300",
        title=f"Approval Required {n}", description="Material: 5000092261", entity_type="RECOMMENDATION", entity_id=f"REC-{n}",
        action_path=f"/inventory-planning/recommendations/REC-{n}?review=1", status="SENT", is_read=is_read, retry_count=0,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    db.add(row)
    db.commit()
    return row


def test_a_user_sees_only_their_own_in_app_notifications(db, api):
    add(db, ME, 1)
    add(db, OTHER, 2)
    add(db, ME, 3, channel="EMAIL")  # an email row is not an inbox entry

    body = api.get(BASE, headers=as_user(ME)).json()

    assert [i["title"] for i in body["items"]] == ["Approval Required 1"]
    assert body["total"] == 1


def test_items_show_what_a_person_needs_and_not_the_internal_module(db, api):
    add(db, ME, 1)

    [item] = api.get(BASE, headers=as_user(ME)).json()["items"]

    assert set(item) == {"id", "title", "description", "eventType", "entityType", "entityId", "plant", "actionPath", "isRead", "createdAt", "readAt"}
    assert item["actionPath"] == "/inventory-planning/recommendations/REC-1?review=1"
    assert item["isRead"] is False and item["readAt"] is None


def test_newest_first_with_paging(db, api):
    for n in range(5):
        add(db, ME, n, minutes_ago=n)  # n=0 is the newest

    first = api.get(f"{BASE}?pageSize=2&page=1", headers=as_user(ME)).json()
    third = api.get(f"{BASE}?pageSize=2&page=3", headers=as_user(ME)).json()

    assert [i["title"] for i in first["items"]] == ["Approval Required 0", "Approval Required 1"]
    assert [i["title"] for i in third["items"]] == ["Approval Required 4"]
    assert first["total"] == 5 and first["page"] == 1 and first["pageSize"] == 2


def test_filters_and_the_unread_count(db, api):
    add(db, ME, 1, is_read=False)
    add(db, ME, 2, is_read=True)
    add(db, ME, 3, is_read=False)
    add(db, OTHER, 4, is_read=False)

    unread = api.get(f"{BASE}?status=unread", headers=as_user(ME)).json()
    read = api.get(f"{BASE}?status=read", headers=as_user(ME)).json()
    everything = api.get(f"{BASE}?status=all", headers=as_user(ME)).json()

    assert unread["total"] == 2 and read["total"] == 1 and everything["total"] == 3
    assert everything["unreadCount"] == 2
    assert api.get(f"{BASE}/unread-count", headers=as_user(ME)).json() == {"unread": 2}
    assert api.get(f"{BASE}?status=bogus", headers=as_user(ME)).status_code == 422


def test_mark_one_as_read(db, api):
    row = add(db, ME, 1)

    response = api.post(f"{BASE}/{row.id}/read", headers=as_user(ME))

    assert response.status_code == 200 and response.json()["isRead"] is True and response.json()["readAt"]
    assert api.get(f"{BASE}/unread-count", headers=as_user(ME)).json() == {"unread": 0}
    assert api.post(f"{BASE}/{row.id}/read", headers=as_user(ME)).status_code == 200  # again is harmless


def test_mark_all_as_read_touches_only_my_notifications(db, api):
    add(db, ME, 1)
    add(db, ME, 2)
    theirs = add(db, OTHER, 3)

    assert api.post(f"{BASE}/read-all", headers=as_user(ME)).json() == {"updated": 2}

    assert api.get(f"{BASE}/unread-count", headers=as_user(ME)).json() == {"unread": 0}
    db.refresh(theirs)
    assert theirs.is_read is False
    assert api.post(f"{BASE}/read-all", headers=as_user(ME)).json() == {"updated": 0}


def test_another_users_notification_cannot_be_read_or_changed_by_guessing_its_id(db, api):
    theirs = add(db, OTHER, 1)

    assert api.get(f"{BASE}/{theirs.id}", headers=as_user(ME)).status_code == 404
    assert api.post(f"{BASE}/{theirs.id}/read", headers=as_user(ME)).status_code == 404
    assert api.get(f"{BASE}/no-such-id", headers=as_user(ME)).status_code == 404
    db.refresh(theirs)
    assert theirs.is_read is False


def test_an_email_row_is_not_reachable_through_the_inbox_even_for_its_recipient(db, api):
    email = add(db, ME, 1, channel="EMAIL")
    assert api.get(f"{BASE}/{email.id}", headers=as_user(ME)).status_code == 404


def test_a_notification_created_by_an_approval_appears_in_the_approvers_inbox(db, api):
    from tests.notifications.test_i07_approvals import SUBMITTER, configure, person, recommendation, submit
    from app.initiatives.i7.recommendations.types import ApprovalRole

    configure(db)
    submit(api, recommendation(db))

    mine = api.get(BASE, headers=as_user(person(ApprovalRole.END_USER))).json()
    assert [i["title"] for i in mine["items"]] == ["Approval Required – Material Recommendation"]
    assert api.get(BASE, headers=as_user(SUBMITTER)).json()["total"] == 0  # the submitter is not asked to approve
    assert db.scalar(select(NotificationRow.module).limit(1)) == "I07"  # kept internally, never served

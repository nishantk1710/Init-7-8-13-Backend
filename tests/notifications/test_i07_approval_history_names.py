"""The approval history names the person behind each ledger entry, from app_user, and never
fails because the directory cannot be read."""

from __future__ import annotations

from sqlalchemy import text

from app.initiatives.i7.recommendations.types import ApprovalRole
from tests.notifications.test_i07_approvals import (
    BASE,
    SUBMITTER,
    act,
    configure,
    person,
    recommendation,
    submit,
)


def history(api, rec_id):
    response = api.get(f"{BASE}/{rec_id}/approval-history")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_each_entry_carries_the_actors_name_beside_the_object_id(db, api):
    configure(db)
    rec_id = recommendation(db)
    submit(api, rec_id)
    assert act(api, rec_id, ApprovalRole.END_USER, "APPROVE").status_code == 200

    items = history(api, rec_id)

    assert [(i["action"], i["actor_id"], i["actor_name"]) for i in items] == [
        ("SUBMIT", SUBMITTER, "Submitter"),
        ("APPROVE", person(ApprovalRole.END_USER), ApprovalRole.END_USER.value),
    ]


def test_someone_who_never_signed_in_has_no_name_and_keeps_the_id(db, api):
    configure(db)
    rec_id = recommendation(db)
    stranger = "77777777-0000-0000-0000-000000000000"
    api.post(f"{BASE}/{rec_id}/submit", json={}, headers={"X-Actor-Id": stranger})

    [entry] = history(api, rec_id)
    assert (entry["actor_id"], entry["actor_name"]) == (stranger, None)


def test_a_database_without_the_directory_table_still_returns_the_history(db, api):
    configure(db)
    rec_id = recommendation(db)
    submit(api, rec_id)
    db.execute(text("DROP TABLE app_user"))
    db.commit()

    [entry] = history(api, rec_id)
    assert (entry["action"], entry["actor_id"], entry["actor_name"]) == ("SUBMIT", SUBMITTER, None)

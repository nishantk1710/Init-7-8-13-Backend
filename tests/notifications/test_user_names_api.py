"""GET /api/users/names: the names behind the object ids a screen holds, from app_user."""

from __future__ import annotations

from app.models.app_user import AppUser

ADA = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"
NEVER_SIGNED_IN = "33333333-3333-3333-3333-333333333333"


def test_known_ids_get_name_and_email_while_unknown_and_placeholder_ids_are_left_out(db, api):
    db.add(AppUser(oid=ADA, name="Ada King", email="ada@example.com"))
    db.add(AppUser(oid=BOB, name="", email="bob@example.com"))
    db.commit()

    response = api.get(
        "/api/users/names",
        params=[("oid", ADA), ("oid", BOB), ("oid", NEVER_SIGNED_IN), ("oid", "UNAUTHENTICATED-FRONTEND"), ("oid", "U1")],
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "names": {
            ADA: {"oid": ADA, "name": "Ada King", "email": "ada@example.com"},
            BOB: {"oid": BOB, "name": "", "email": "bob@example.com"},
        }
    }


def test_the_latest_sign_in_wins_and_asking_for_nothing_returns_nothing(db, api):
    db.add(AppUser(oid=ADA, name="Ada", email="ada@example.com"))
    db.commit()
    db.add(AppUser(oid=ADA, name="Ada King", email="ada@example.com"))
    db.commit()

    assert api.get("/api/users/names", params={"oid": ADA}).json()["names"][ADA]["name"] == "Ada King"
    assert api.get("/api/users/names").json() == {"names": {}}

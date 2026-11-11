"""Recipient rows that follow the roles in each sign-in token (token_recipients)."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.security import Roles
from app.integrations.notifications import token_recipients
from app.integrations.notifications.config_store import RECIPIENTS_TABLE
from app.integrations.notifications.resolver import resolve_role
from app.integrations.notifications.token_recipients import SOURCE, sync_roles
from app.models.notification import NotificationConfigAudit, NotificationRecipient

OID = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


def rows(db, oid=OID):
    return list(
        db.scalars(
            select(NotificationRecipient).where(NotificationRecipient.user_oid == oid).order_by(NotificationRecipient.id)
        )
    )


def add(db, **fields):
    row = NotificationRecipient(**{"active": True, **fields})
    db.add(row)
    db.commit()
    return row


# --- sync_roles: the rules ---------------------------------------------------------------------


def test_first_sign_in_adds_one_all_plant_row_per_held_role(db):
    result = sync_roles(db, OID, [Roles.ENGINEERING_HEAD, Roles.REQUESTER, Roles.VIEWER])

    assert (result.added, result.reactivated, result.deactivated) == (3, 0, 0)
    assert {(r.role, r.plant, r.active, r.created_by) for r in rows(db)} == {
        (Roles.ENGINEERING_HEAD, None, True, SOURCE),
        (Roles.REQUESTER, None, True, SOURCE),
        (Roles.VIEWER, None, True, SOURCE),
    }
    # usable by the resolver at any plant
    assert resolve_role(db, Roles.ENGINEERING_HEAD, "1500")[0].user_oid == OID
    audits = list(db.scalars(select(NotificationConfigAudit)))
    assert {(a.table_name, a.action, a.actor) for a in audits} == {(RECIPIENTS_TABLE, "CREATE", SOURCE)}
    assert len(audits) == 3


def test_a_second_sign_in_with_the_same_roles_changes_nothing(db):
    sync_roles(db, OID, [Roles.PLANT_HEAD])
    assert not sync_roles(db, OID, [Roles.PLANT_HEAD]).changed
    assert len(rows(db)) == 1


def test_an_administrators_plant_row_already_covers_the_role_so_no_all_plant_row_is_added(db):
    add(db, role=Roles.PLANT_HEAD, plant="1300", user_oid=OID, created_by="admin-1")
    assert not sync_roles(db, OID, [Roles.PLANT_HEAD]).changed
    assert [(r.plant, r.active) for r in rows(db)] == [("1300", True)]


def test_a_role_that_left_the_token_switches_off_every_row_for_it_even_an_administrators(db):
    admin_row = add(db, role=Roles.PLANT_HEAD, plant="1300", user_oid=OID, created_by="admin-1")
    mine = add(db, role=Roles.PLANT_HEAD, plant=None, user_oid=OID, created_by=SOURCE)

    result = sync_roles(db, OID, [Roles.VIEWER])

    db.refresh(admin_row)
    db.refresh(mine)
    assert (result.added, result.deactivated) == (1, 2)
    assert admin_row.active is False and admin_row.updated_by == SOURCE
    assert mine.active is False and mine.updated_by == SOURCE
    assert resolve_role(db, Roles.PLANT_HEAD, "1300")[0].error
    actions = [(a.action, a.actor) for a in db.scalars(select(NotificationConfigAudit))]
    assert sorted(actions) == [("CREATE", SOURCE), ("UPDATE", SOURCE), ("UPDATE", SOURCE)]


def test_a_role_that_comes_back_switches_the_rows_this_module_turned_off_back_on_keeping_their_plant(db):
    admin_row = add(db, role=Roles.PLANT_HEAD, plant="1300", user_oid=OID, created_by="admin-1")
    sync_roles(db, OID, [])
    db.refresh(admin_row)
    assert admin_row.active is False

    result = sync_roles(db, OID, [Roles.PLANT_HEAD])

    db.refresh(admin_row)
    assert (result.added, result.reactivated) == (0, 1)
    assert admin_row.active is True and admin_row.plant == "1300" and admin_row.updated_by == SOURCE
    assert len(rows(db)) == 1


def test_a_row_an_administrator_retired_stays_off_and_is_not_replaced(db):
    add(
        db,
        role=Roles.PLANT_HEAD,
        plant="1300",
        user_oid=OID,
        active=False,
        created_by="admin-1",
        updated_by="admin-1",
    )

    assert not sync_roles(db, OID, [Roles.PLANT_HEAD]).changed
    assert [r.active for r in rows(db)] == [False]
    assert resolve_role(db, Roles.PLANT_HEAD, "1300")[0].error


def test_roles_the_app_does_not_know_other_people_and_unknown_role_rows_are_left_alone(db):
    other = add(db, role=Roles.PLANT_HEAD, plant="1300", user_oid=OTHER)
    legacy = add(db, role="Spares.Legacy", plant=None, user_oid=OID)

    result = sync_roles(db, OID, ["Spares.NotARole", Roles.SAP_EVENTS_WRITE])

    db.refresh(other)
    db.refresh(legacy)
    assert not result.changed
    assert other.active is True and legacy.active is True
    assert rows(db, OTHER) == [other]


# --- remember: when the sign-in hook does anything ---------------------------------------------


@dataclass(frozen=True)
class User:
    oid: str
    roles: frozenset[str]
    is_application: bool = False
    authenticated: bool = True


class _RunNow:
    """Stands in for the background executor so a test sees the call at once."""

    def submit(self, fn, *args):
        fn(*args)


def _settings(enabled: bool = True):
    return lambda: SimpleNamespace(database_url="sqlite://", notification_recipients_from_token=enabled)


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple[str, frozenset[str]]] = []
    token_recipients.forget_all()
    monkeypatch.setattr(token_recipients, "get_settings", _settings())
    monkeypatch.setattr(token_recipients, "_writer", _RunNow())
    monkeypatch.setattr(token_recipients, "_sync_in_background", lambda oid, roles: seen.append((oid, roles)))
    return seen


def test_remember_syncs_once_per_role_set_and_again_when_the_roles_change(calls):
    user = User(oid=OID, roles=frozenset({Roles.PLANT_HEAD}))
    token_recipients.remember(user)
    token_recipients.remember(user)
    token_recipients.remember(User(oid=OID, roles=frozenset({Roles.PLANT_HEAD, Roles.VIEWER})))

    assert calls == [
        (OID, frozenset({Roles.PLANT_HEAD})),
        (OID, frozenset({Roles.PLANT_HEAD, Roles.VIEWER})),
    ]


def test_remember_ignores_applications_signed_off_users_and_ids_that_are_not_entra_object_ids(calls):
    token_recipients.remember(User(oid=OID, roles=frozenset({Roles.PLANT_HEAD}), is_application=True))
    token_recipients.remember(User(oid=OID, roles=frozenset({Roles.PLANT_HEAD}), authenticated=False))
    token_recipients.remember(User(oid="alice", roles=frozenset({Roles.PLANT_HEAD})))
    assert calls == []


def test_remember_does_nothing_when_switched_off(calls, monkeypatch):
    monkeypatch.setattr(token_recipients, "get_settings", _settings(enabled=False))
    token_recipients.remember(User(oid=OID, roles=frozenset({Roles.PLANT_HEAD})))
    assert calls == []


def test_a_failed_sync_is_tried_again_on_the_next_request(monkeypatch):
    import app.core.db as core_db

    token_recipients.forget_all()
    monkeypatch.setattr(token_recipients, "get_settings", _settings())
    monkeypatch.setattr(token_recipients, "_writer", _RunNow())
    attempts: list[int] = []

    def no_database():
        attempts.append(1)
        raise RuntimeError("no database")

    monkeypatch.setattr(core_db, "get_sessionmaker", no_database)

    user = User(oid=OID, roles=frozenset({Roles.PLANT_HEAD}))
    token_recipients.remember(user)
    token_recipients.remember(user)
    assert len(attempts) == 2  # the failure was forgotten, so the second request tried again

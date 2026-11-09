"""POST .../actions refuses an approval role the signed-in caller's token does not hold.

Sign-in itself is simulated by replacing ``security.current_user`` with a user who holds exactly the
roles given; the actor id still comes from the X-Actor-Id header (the AUTH_ENABLED=false path).
"""

from __future__ import annotations

import pytest

from app.core import security
from app.core.security import CurrentUser, Roles
from app.initiatives.i7.recommendations.types import ApprovalRole
from tests.notifications.test_i07_approvals import BASE, SUBMITTER, act, configure, person, recommendation, submit


def sign_in_with(monkeypatch, *roles: str) -> None:
    user = CurrentUser(oid="33333333-3333-3333-3333-333333333333", roles=frozenset(roles), authenticated=True)
    monkeypatch.setattr(security, "current_user", lambda request: user)


def test_a_role_the_token_does_not_hold_is_refused(db, api, monkeypatch):
    configure(db)
    rec = recommendation(db)
    submit(api, rec)

    sign_in_with(monkeypatch, Roles.ENGINEERING_MANAGER)
    response = act(api, rec, ApprovalRole.END_USER, "APPROVE")

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "ROLE_NOT_HELD"


def test_a_user_with_no_approval_role_is_refused(db, api, monkeypatch):
    configure(db)
    rec = recommendation(db)
    submit(api, rec)

    sign_in_with(monkeypatch, Roles.VIEWER)
    response = act(api, rec, ApprovalRole.END_USER, "APPROVE")

    assert response.status_code == 403, response.text


def test_the_role_the_token_holds_is_allowed(db, api, monkeypatch):
    configure(db)
    rec = recommendation(db)
    submit(api, rec)

    sign_in_with(monkeypatch, Roles.END_USER)
    response = act(api, rec, ApprovalRole.END_USER, "APPROVE")

    assert response.status_code == 200, response.text
    assert response.json()["pending_role"] == ApprovalRole.ENGINEERING_MANAGER.value


def test_the_inventory_controller_step_needs_the_approver_role(db, api, monkeypatch):
    configure(db)
    rec = recommendation(db, is_oar=True)
    submit(api, rec)

    sign_in_with(monkeypatch, Roles.INVENTORY_CONTROLLER)  # the older role is not an approval stage
    response = act(api, rec, ApprovalRole.INVENTORY_CONTROLLER, "APPROVE")
    assert response.status_code == 403, response.text

    sign_in_with(monkeypatch, Roles.INVENTORY_CONTROLLER_APPROVER)
    response = act(api, rec, ApprovalRole.INVENTORY_CONTROLLER, "APPROVE")
    assert response.status_code == 200, response.text

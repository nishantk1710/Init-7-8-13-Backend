"""Approval of a Min-Max -> OAR conversion request: the same chain and rules as an OAR conversion.

An in-memory SQLite database holds the four ``i7_reverse_*`` tables.
"""

from types import SimpleNamespace

import pytest
from reverse_conversion_support import FakeSource, population
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.initiatives.i7.recommendations.types import (
    OAR_APPROVAL_CHAIN,
    ApprovalAction as Action,
    ApprovalRole as Role,
    WorkflowError,
)
from app.initiatives.i7.reverse_approval import service
from app.initiatives.i7.reverse_approval.service import RequestError
from app.initiatives.i7.reverse_conversion import snapshot
from app.initiatives.i7.reverse_conversion.snapshot import find_stored, latest_request_briefs, run_reverse_conversion
from app.models.base import Base
from app.models.i7_reverse import (
    ReverseApprovalLedger,
    ReverseConversionRequest,
    ReverseRecommendation,
    ReverseRun,
)

TABLES = [ReverseRun.__table__, ReverseRecommendation.__table__, ReverseConversionRequest.__table__, ReverseApprovalLedger.__table__]


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(
        snapshot, "get_settings", lambda: SimpleNamespace(i7_reverse_anchor_month="2026-10", i7_reverse_anchor_source="configured", i7_rationale_concurrency=2)
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=TABLES)
    factory = sessionmaker(bind=engine)
    run_reverse_conversion(session_factory=factory, source_factory=lambda s: FakeSource(*population()))
    with factory() as db:
        yield db


def raise_request(session, material="REC1", plant="1300", comment=None):
    run, stored = find_stored(session, material, plant)
    request = service.submit_request(session, stored, run, actor_id="planner-1", comment=comment)
    session.commit()
    return request


def act(session, request, role, action, comment=None):
    service.apply_action(session, request, actor_id=f"{role.name}-1", actor_role=role, action=action, comment=comment)
    session.commit()
    return request


def ledger(session, request):
    return [(e.action, e.actor_role, e.previous_status, e.new_status) for e in service.history(session, request.id)]


# --- The chain is the OAR conversion's own ----------------------------------------------------------


def test_the_route_is_exactly_the_fixed_oar_chain():
    assert service.ROUTE == OAR_APPROVAL_CHAIN
    assert [r.value for r in service.ROUTE] == ["Inventory Controller", "Engineering Head", "Plant Head", "Commercial Head"]


def test_a_new_request_waits_for_the_first_role_and_is_ledgered(session):
    request = raise_request(session, comment="Dormant for two years")
    assert (request.status, request.chain_index, request.pending_role) == ("PENDING_APPROVAL", 0, "Inventory Controller")
    assert request.submitted_by == "planner-1" and request.submit_comment == "Dormant for two years"
    assert ledger(session, request) == [("SUBMIT", "Requester", "READY_FOR_REVIEW", "PENDING_APPROVAL")]


def test_what_the_approvers_were_shown_is_frozen_on_the_request(session):
    request = raise_request(session)
    run, stored = find_stored(session, "REC1", "1300")
    assert (request.reason, request.detail, request.confidence, request.run_id) == (stored.reason, stored.detail, stored.confidence, run.id)
    assert request.anchor_month == run.anchor_month


def test_the_four_roles_approving_in_order_end_at_sap_execution_pending(session):
    request = raise_request(session)
    for role in OAR_APPROVAL_CHAIN:
        assert request.pending_role == role.value
        act(session, request, role, Action.APPROVE)
    assert (request.status, request.pending_role) == ("SAP_EXECUTION_PENDING", None)
    assert [e[0] for e in ledger(session, request)] == ["SUBMIT"] + ["APPROVE"] * 4
    assert ledger(session, request)[-1][3] == "SAP_EXECUTION_PENDING"  # it stops here: nothing reaches SAP


# --- The rules --------------------------------------------------------------------------------------


def test_a_role_that_is_not_pending_cannot_act_and_no_stage_can_be_skipped(session):
    request = raise_request(session)
    with pytest.raises(WorkflowError):
        act(session, request, Role.PLANT_HEAD, Action.APPROVE)  # skipping to the third stage
    with pytest.raises(WorkflowError):
        act(session, request, Role.COMMERCIAL_HEAD, Action.APPROVE)
    assert request.chain_index == 0 and len(ledger(session, request)) == 1


@pytest.mark.parametrize("action", [Action.REJECT, Action.SEND_BACK, Action.HOLD])
def test_reject_send_back_and_hold_need_a_comment(session, action):
    request = raise_request(session)
    with pytest.raises(WorkflowError, match="comment"):
        act(session, request, Role.INVENTORY_CONTROLLER, action)
    with pytest.raises(WorkflowError, match="comment"):
        act(session, request, Role.INVENTORY_CONTROLLER, action, comment="   ")


def test_reject_ends_the_request_and_a_new_one_may_then_be_raised(session):
    request = raise_request(session)
    act(session, request, Role.INVENTORY_CONTROLLER, Action.REJECT, "Still needed in stock")
    assert (request.status, request.pending_role) == ("REJECTED", None)
    again = raise_request(session)
    assert again.id != request.id and again.status == "PENDING_APPROVAL"


def test_send_back_returns_one_step(session):
    request = raise_request(session)
    act(session, request, Role.INVENTORY_CONTROLLER, Action.APPROVE)
    act(session, request, Role.ENGINEERING_HEAD, Action.SEND_BACK, "Check the lead time")
    assert (request.status, request.pending_role) == ("SENT_BACK", "Inventory Controller")
    act(session, request, Role.INVENTORY_CONTROLLER, Action.APPROVE)
    assert request.pending_role == "Engineering Head"


def test_hold_freezes_the_request_until_the_same_role_releases_it(session):
    request = raise_request(session)
    act(session, request, Role.INVENTORY_CONTROLLER, Action.HOLD, "Waiting for the stock count")
    assert (request.status, request.pending_role) == ("HELD", "Inventory Controller")
    with pytest.raises(WorkflowError):
        act(session, request, Role.INVENTORY_CONTROLLER, Action.APPROVE)
    act(session, request, Role.INVENTORY_CONTROLLER, Action.RELEASE_HOLD)
    assert (request.status, request.pending_role) == ("PENDING_APPROVAL", "Inventory Controller")


def test_adjust_does_not_apply_to_a_conversion(session):
    request = raise_request(session)
    with pytest.raises(WorkflowError, match="ADJUST"):
        act(session, request, Role.INVENTORY_CONTROLLER, Action.ADJUST, "x")


def test_a_finished_request_accepts_no_more_actions(session):
    request = raise_request(session)
    for role in OAR_APPROVAL_CHAIN:
        act(session, request, role, Action.APPROVE)
    with pytest.raises(WorkflowError):
        act(session, request, Role.COMMERCIAL_HEAD, Action.APPROVE)


# --- Who may be asked for, and how often ------------------------------------------------------------


def test_a_material_that_is_not_eligible_cannot_be_requested(session):
    run, stored = find_stored(session, "PROT", "1300")
    with pytest.raises(RequestError) as caught:
        service.submit_request(session, stored, run, actor_id="planner-1", comment=None)
    assert caught.value.code == "REVERSE_NOT_ELIGIBLE"


def test_only_one_request_can_be_open_for_a_material_plant(session):
    raise_request(session)
    with pytest.raises(RequestError) as caught:
        raise_request(session)
    assert caught.value.code == "REVERSE_REQUEST_OPEN"


def test_a_fully_approved_request_still_blocks_another_until_sap_is_changed(session):
    request = raise_request(session)
    for role in OAR_APPROVAL_CHAIN:
        act(session, request, role, Action.APPROVE)
    with pytest.raises(RequestError):
        raise_request(session)


def test_two_material_plants_have_separate_requests(session):
    first, second = raise_request(session, "REC1", "1300"), raise_request(session, "REC2", "1300")
    assert first.id != second.id
    assert service.latest_request(session, "REC1", "1300").id == first.id


# --- What a list shows ------------------------------------------------------------------------------


def test_a_list_row_shows_the_status_and_who_it_waits_for_without_the_approval_code(session):
    request = raise_request(session)
    assert latest_request_briefs(session, [("REC1", "1300")]) == {("REC1", "1300"): (request.id, "PENDING_APPROVAL", "Inventory Controller")}
    act(session, request, Role.INVENTORY_CONTROLLER, Action.APPROVE)
    assert latest_request_briefs(session, [("REC1", "1300")])[("REC1", "1300")][2] == "Engineering Head"
    for role in OAR_APPROVAL_CHAIN[1:]:
        act(session, request, role, Action.APPROVE)
    assert latest_request_briefs(session, [("REC1", "1300")])[("REC1", "1300")] == (request.id, "SAP_EXECUTION_PENDING", None)


def test_every_action_leaves_one_ledger_row_in_order(session):
    request = raise_request(session)
    act(session, request, Role.INVENTORY_CONTROLLER, Action.HOLD, "wait")
    act(session, request, Role.INVENTORY_CONTROLLER, Action.RELEASE_HOLD)
    act(session, request, Role.INVENTORY_CONTROLLER, Action.REJECT, "no")
    assert [e[0] for e in ledger(session, request)] == ["SUBMIT", "HOLD", "RELEASE_HOLD", "REJECT"]
    assert len(list(session.scalars(select(ReverseApprovalLedger)))) == 4


# --- Declining without sending it for approval ------------------------------------------------------


def decline(session, material="REC1", plant="1300", comment="Still needed in stock"):
    run, stored = find_stored(session, material, plant)
    request = service.decline_request(session, stored, run, actor_id="planner-1", comment=comment)
    session.commit()
    return request


def test_declining_ends_in_rejected_with_nobody_pending_and_one_ledger_row(session):
    request = decline(session)
    assert (request.status, request.pending_role, request.chain_index) == ("REJECTED", None, 0)
    assert request.submit_comment == "Still needed in stock"
    assert ledger(session, request) == [("REJECT", "Requester", "READY_FOR_REVIEW", "REJECTED")]


@pytest.mark.parametrize("comment", [None, "", "   "])
def test_declining_needs_a_reason(session, comment):
    run, stored = find_stored(session, "REC1", "1300")
    with pytest.raises(WorkflowError, match="comment"):
        service.decline_request(session, stored, run, actor_id="planner-1", comment=comment)
    assert service.latest_request(session, "REC1", "1300") is None  # nothing was recorded


def test_a_declined_material_can_be_asked_for_again(session):
    declined = decline(session)
    again = raise_request(session)
    assert again.id != declined.id and again.status == "PENDING_APPROVAL"


def test_a_material_in_the_chain_cannot_be_declined_this_way(session):
    raise_request(session)
    with pytest.raises(RequestError) as caught:
        decline(session)
    assert caught.value.code == "REVERSE_REQUEST_OPEN"


def test_a_material_that_is_not_eligible_cannot_be_declined_either(session):
    run, stored = find_stored(session, "PROT", "1300")
    with pytest.raises(RequestError) as caught:
        service.decline_request(session, stored, run, actor_id="planner-1", comment="x")
    assert caught.value.code == "REVERSE_NOT_ELIGIBLE"

"""Declining a recommendation from the Recommendations page, without sending it for approval.

The state machine rule is pure; the ledger write runs against SQLite with only the two tables it
touches.
"""

from __future__ import annotations

import pytest
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.i7 import approvals as approvals_api
from app.initiatives.i7.recommendations import ledger, workflow
from app.initiatives.i7.recommendations.types import (
    ApprovalAction,
    ApprovalRole,
    LifecycleStatus,
    WorkflowError,
)
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation
from app.schemas.i7.approvals import RejectRequest


# --- the state machine ----------------------------------------------------------------------


def state(status: LifecycleStatus) -> workflow.WorkflowState:
    return workflow.WorkflowState(status=status, chain_index=0)


def test_a_ready_for_review_recommendation_can_be_declined_with_a_reason():
    new = workflow.reject_unsubmitted(state(LifecycleStatus.READY_FOR_REVIEW), "not needed at this plant")
    assert new.status is LifecycleStatus.REJECTED
    assert new.pending_role is None  # nothing is left for an approver


@pytest.mark.parametrize("comment", [None, "", "   "])
def test_declining_needs_a_reason(comment):
    with pytest.raises(WorkflowError, match="non-empty comment"):
        workflow.reject_unsubmitted(state(LifecycleStatus.READY_FOR_REVIEW), comment)


@pytest.mark.parametrize(
    "status",
    [
        LifecycleStatus.NOT_EVALUABLE,
        LifecycleStatus.PENDING_APPROVAL,
        LifecycleStatus.SENT_BACK,
        LifecycleStatus.HELD,
        LifecycleStatus.APPROVED,
        LifecycleStatus.REJECTED,
    ],
)
def test_only_an_unsubmitted_recommendation_can_be_declined_this_way(status):
    """Once it is in the chain, REJECT is the pending role's -- never a planner's shortcut."""
    with pytest.raises(WorkflowError, match="READY_FOR_REVIEW"):
        workflow.reject_unsubmitted(state(status), "reason")


def test_the_chain_still_refuses_a_reject_before_submission():
    with pytest.raises(WorkflowError):
        workflow.apply_action(
            state(LifecycleStatus.READY_FOR_REVIEW), ApprovalAction.REJECT, ApprovalRole.END_USER, "reason"
        )


# --- the ledger write -----------------------------------------------------------------------


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (Recommendation, ApprovalLedgerEntry):
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()  # the models declare some indexes twice; SQLite refuses that
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def recommendation(status=LifecycleStatus.READY_FOR_REVIEW) -> Recommendation:
    return Recommendation(
        recommendation_id="REC-STD-M1-1300",
        sap_material_number="M1",
        sap_plant_code="1300",
        feature_run_id=1,
        forecast_run_id=1,
        inventory_run_id=1,
        oar_run_id=1,
        policy_id="p",
        policy_version=1,
        formula_version="f",
        status=status.value,
        impact_status="X",
        is_oar=False,
    )


def entries(db):
    return db.execute(select(ApprovalLedgerEntry)).scalars().all()


def test_declining_sets_rejected_and_writes_one_reject_entry(db):
    row = recommendation()
    db.add(row)
    db.flush()
    ledger.reject_before_submission(db, row, "planner-1", "stock is held centrally")
    db.commit()

    assert row.status == "REJECTED"
    (entry,) = entries(db)
    assert (entry.action, entry.previous_status, entry.new_status) == ("REJECT", "READY_FOR_REVIEW", "REJECTED")
    assert entry.actor_id == "planner-1"
    assert entry.comment == "stock is held centrally"


def test_a_refused_decline_writes_nothing(db):
    row = recommendation(LifecycleStatus.PENDING_APPROVAL)
    db.add(row)
    db.flush()
    with pytest.raises(WorkflowError):
        ledger.reject_before_submission(db, row, "planner-1", "reason")
    assert row.status == "PENDING_APPROVAL"
    assert entries(db) == []


def test_a_decline_without_a_reason_writes_nothing(db):
    row = recommendation()
    db.add(row)
    db.flush()
    with pytest.raises(WorkflowError):
        ledger.reject_before_submission(db, row, "planner-1", "  ")
    assert row.status == "READY_FOR_REVIEW"
    assert entries(db) == []


def test_the_endpoint_returns_the_new_state_and_has_no_pending_role(db):
    row = recommendation()
    db.add(row)
    db.commit()
    response = approvals_api.reject_recommendation(
        "REC-STD-M1-1300", RejectRequest(actor_id="planner-1", comment="not needed"), db
    )
    assert response.status == "REJECTED"
    assert response.pending_role is None


def test_the_endpoint_turns_a_workflow_error_into_a_409(db):
    from fastapi import HTTPException

    row = recommendation(LifecycleStatus.APPROVED)
    db.add(row)
    db.commit()
    with pytest.raises(HTTPException) as caught:
        approvals_api.reject_recommendation(
            "REC-STD-M1-1300", RejectRequest(actor_id="planner-1", comment="reason"), db
        )
    assert caught.value.status_code == 409

"""Approval workflow endpoints.

Every rule -- correct role, no stage skipping, comment required for REJECT /
SEND_BACK / ADJUST, immutable ledger entry -- is enforced by
``app.initiatives.i7.recommendations.workflow`` and ``ledger``, not
re-implemented here. A route's job is to load the recommendation, call the
existing service, translate ``WorkflowError`` into HTTP 409, and commit.

One generic ``POST .../actions`` endpoint rather than five near-identical
routes: APPROVE/REJECT/SEND_BACK/ADJUST share the same validation and the same
ledger write, and duplicating that across five functions is exactly the kind
of duplicated business logic the phase brief asks to avoid. The action is an
explicit enum in the request body, not a free string.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.i7.deps import get_session, load_latest_recommendation
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import ledger, routing, workflow
from app.initiatives.i7.recommendations.types import WorkflowError
from app.models.i7_recommendation import Recommendation
from app.schemas.i7.approvals import (
    ApprovalActionRequest,
    SubmitRequest,
    WorkflowStateResponse,
)
from app.schemas.i7.errors import conflict

router = APIRouter(tags=["i7-approvals"])


def _state_response(row: Recommendation, policy: PolicyDocument) -> WorkflowStateResponse:
    route = routing.route_for(row.is_oar, row.criticality, policy)
    state = workflow.WorkflowState(
        status=row.status,
        chain_index=row.chain_index,
        adjustment_count=row.adjustment_count,
        route=route,
    )
    pending = state.pending_role
    return WorkflowStateResponse(
        recommendation_id=row.recommendation_id,
        status=row.status,
        pending_role=pending.value if pending else None,
        route=[role.value for role in route],
        chain_index=row.chain_index,
        adjustment_count=row.adjustment_count,
        current_version=row.current_version,
    )


@router.post(
    "/recommendations/{recommendation_id}/submit",
    response_model=WorkflowStateResponse,
    summary="Submit a recommendation for approval",
    description="Enters the recommendation's own route at its first role -- "
    "the fixed OAR chain (Inventory Controller -> Commercial Head -> "
    "Engineering Head -> Plant Head) for OAR conversion recommendations, or "
    "the criticality-routed ROP/Max chain otherwise. Requires the "
    "recommendation to be READY_FOR_REVIEW -- a NOT_EVALUABLE recommendation "
    "cannot be submitted.",
    responses={404: {"description": "Recommendation not found"}, 409: {"description": "Invalid workflow state"}},
)
def submit_recommendation(
    recommendation_id: str,
    body: SubmitRequest,
    session: Annotated[Session, Depends(get_session)],
) -> WorkflowStateResponse:
    row = load_latest_recommendation(session, recommendation_id)
    policy = PolicyDocument()
    try:
        ledger.submit_for_approval(session, row, actor_id=body.actor_id, policy=policy)
    except WorkflowError as exc:
        raise conflict("INVALID_WORKFLOW_STATE", str(exc)) from exc
    session.commit()
    return _state_response(row, policy)


@router.post(
    "/recommendations/{recommendation_id}/actions",
    response_model=WorkflowStateResponse,
    summary="Apply an approval action (APPROVE / REJECT / SEND_BACK / HOLD / RELEASE_HOLD / ADJUST)",
    description="Enforces the recommendation's own route (see /submit): the "
    "acting role must match the pending role exactly, stages cannot be "
    "skipped, and REJECT / SEND_BACK / ADJUST / HOLD require a non-empty "
    "comment. HOLD freezes the recommendation at its current step -- it "
    "does not advance or reverse -- until the same pending role calls "
    "RELEASE_HOLD; this is distinct from SEND_BACK, which returns one step "
    "for correction/rework. Final APPROVE at the route's last role moves "
    "the recommendation to SAP_EXECUTION_PENDING and calls no SAP API. "
    "Every action writes one immutable approval-ledger entry.",
    responses={404: {"description": "Recommendation not found"}, 409: {"description": "Invalid action for the current role/stage, or missing required comment"}},
)
def apply_action(
    recommendation_id: str,
    body: ApprovalActionRequest,
    session: Annotated[Session, Depends(get_session)],
) -> WorkflowStateResponse:
    row = load_latest_recommendation(session, recommendation_id)
    policy = PolicyDocument()
    try:
        ledger.apply_approval_action(
            session, row, body.actor_id, body.actor_role, body.action, body.comment,
            policy=policy,
        )
    except WorkflowError as exc:
        raise conflict("INVALID_WORKFLOW_ACTION", str(exc)) from exc
    session.commit()
    return _state_response(row, policy)

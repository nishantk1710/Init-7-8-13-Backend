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

Who is acting
-------------
The actor recorded in the ledger -- and so the submitter and the people notified -- is the
authenticated caller (``get_i7_actor``: the Entra object id from the token, or the
``X-Actor-Id`` header while sign-in is off). The ``actor_id`` field some clients still send in the
body is ignored.

``POST .../actions`` checks ``actor_role`` against the roles in the token: the caller must hold an
approval-stage role, and the one they claim in the body (403 ``ROLE_NOT_HELD`` otherwise).

**Still open:** ``/submit`` and ``/reject`` declare no roles yet (``tests/test_write_roles.py`` PENDING);
which roles may submit or decline before the chain is a business decision.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.i7.actor import Actor, get_i7_actor
from app.api.i7.deps import get_session, load_latest_recommendation
from app.core.security import APPROVAL_STAGE_ROLES, CurrentUser, require_roles
from app.initiatives.i7.notifications import APPROVAL_ROLE_TO_ENTRA, notify_transition
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import ledger, routing, workflow
from app.initiatives.i7.recommendations.types import ApprovalAction, WorkflowError
from app.models.i7_recommendation import Recommendation
from app.schemas.i7.approvals import (
    ApprovalActionRequest,
    RejectRequest,
    SubmitRequest,
    WorkflowStateResponse,
)
from app.schemas.i7.errors import conflict, forbidden

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


@router.get(
    "/recommendations/{recommendation_id}/workflow-state",
    response_model=WorkflowStateResponse,
    summary="Get the current workflow state (read-only)",
    description="The same route/pending-role computation POST .../submit "
    "and POST .../actions already return, exposed as a plain GET for "
    "callers that only need to display current state (e.g. an approvals "
    "queue) without taking an action. Computes nothing new -- calls the "
    "exact same _state_response the mutating endpoints use.",
    responses={404: {"description": "Recommendation not found"}},
)
def get_workflow_state(
    recommendation_id: str, session: Annotated[Session, Depends(get_session)]
) -> WorkflowStateResponse:
    row = load_latest_recommendation(session, recommendation_id)
    policy = PolicyDocument()
    return _state_response(row, policy)


@router.post(
    "/recommendations/{recommendation_id}/submit",
    response_model=WorkflowStateResponse,
    summary="Submit a recommendation for approval",
    description="Enters the recommendation's own route at its first role -- "
    "the fixed OAR chain (Inventory Controller -> Engineering Head -> "
    "Plant Head -> Commercial Head) for OAR conversion recommendations, or "
    "the criticality-routed ROP/Max chain otherwise. Requires the "
    "recommendation to be READY_FOR_REVIEW -- a NOT_EVALUABLE recommendation "
    "cannot be submitted.",
    responses={404: {"description": "Recommendation not found"}, 409: {"description": "Invalid workflow state"}},
)
def submit_recommendation(
    recommendation_id: str,
    body: SubmitRequest,
    session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(get_i7_actor)],
) -> WorkflowStateResponse:
    row = load_latest_recommendation(session, recommendation_id)
    policy = PolicyDocument()
    try:
        ledger.submit_for_approval(session, row, actor_id=actor.id, policy=policy)
    except WorkflowError as exc:
        raise conflict("INVALID_WORKFLOW_STATE", str(exc)) from exc
    notify_transition(session, row, policy)
    session.commit()
    return _state_response(row, policy)


@router.post(
    "/recommendations/{recommendation_id}/reject",
    response_model=WorkflowStateResponse,
    summary="Reject a recommendation without sending it for approval",
    description="For a planner who does not want to adopt a recommendation: declines it straight "
    "from the Recommendations page instead of submitting it only to have an approver reject it. "
    "Only a READY_FOR_REVIEW recommendation may be declined this way -- once it is in the approval "
    "chain, REJECT belongs to the pending role (POST .../actions). A non-empty comment is "
    "required. Ends in REJECTED, writes one immutable approval-ledger entry, and calls no SAP API.",
    responses={404: {"description": "Recommendation not found"}, 409: {"description": "Not READY_FOR_REVIEW, or no comment"}},
)
def reject_recommendation(
    recommendation_id: str,
    body: RejectRequest,
    session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(get_i7_actor)],
) -> WorkflowStateResponse:
    row = load_latest_recommendation(session, recommendation_id)
    policy = PolicyDocument()
    try:
        ledger.reject_before_submission(session, row, actor_id=actor.id, comment=body.comment, policy=policy)
    except WorkflowError as exc:
        raise conflict("INVALID_WORKFLOW_ACTION", str(exc)) from exc
    notify_transition(session, row, policy)
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
    "for correction/rework. ADJUST carries the new reorder point, safety stock "
    "and maximum stock (new_rop / new_safety_stock / new_max_stock), replaces the "
    "recommended values with them and moves the recommendation to the next role; "
    "the last role in a route cannot adjust. Final APPROVE at the route's last role moves "
    "the recommendation to SAP_EXECUTION_PENDING and calls no SAP API. "
    "Every action writes one immutable approval-ledger entry.",
    responses={404: {"description": "Recommendation not found"}, 409: {"description": "Invalid action for the current role/stage, or missing required comment"}},
)
def apply_action(
    recommendation_id: str,
    body: ApprovalActionRequest,
    session: Annotated[Session, Depends(get_session)],
    actor: Annotated[Actor, Depends(get_i7_actor)],
    user: Annotated[CurrentUser, Depends(require_roles(*sorted(APPROVAL_STAGE_ROLES)))],
) -> WorkflowStateResponse:
    # The claimed role must be one the signed-in caller really holds (from the token), not just any
    # approval role. With sign-in off, `user` holds every role, so this never blocks.
    claimed = APPROVAL_ROLE_TO_ENTRA.get(body.actor_role)
    if claimed is None or not user.has_any(claimed):
        raise forbidden(
            "ROLE_NOT_HELD",
            f"You do not hold the {body.actor_role} approval role.",
            actor_role=str(body.actor_role),
        )
    row = load_latest_recommendation(session, recommendation_id)
    policy = PolicyDocument()
    try:
        adjusted = None
        if body.action is ApprovalAction.ADJUST and None not in (
            body.new_rop, body.new_safety_stock, body.new_max_stock
        ):
            adjusted = ledger.AdjustedValues(
                rop=body.new_rop, safety_stock=body.new_safety_stock, max_stock=body.new_max_stock
            )
        ledger.apply_approval_action(
            session, row, actor.id, body.actor_role, body.action, body.comment,
            policy=policy, adjusted=adjusted,
        )
    except WorkflowError as exc:
        raise conflict("INVALID_WORKFLOW_ACTION", str(exc)) from exc
    notify_transition(session, row, policy)
    session.commit()
    return _state_response(row, policy)

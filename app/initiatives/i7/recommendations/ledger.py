"""The append-only approval ledger, and workflow actions applied through it.

Every call here does two things atomically: apply the pure state machine in
:mod:`workflow`, and write one immutable ledger row recording exactly what
happened. The two never happen separately -- there is no code path that
changes a recommendation's status without a corresponding ledger entry, and no
code path that writes a ledger entry without the state actually changing.

**The route is resolved once, from the recommendation's own facts.** OAR
recommendations always take ``OAR_APPROVAL_CHAIN``; ROP/Max recommendations
are routed by criticality tier through ``PolicyDocument.approval_routing``
(see :mod:`routing`). Neither this module nor the API layer branches on
``is_oar``/``criticality`` itself -- they ask :func:`routing.route_for` and
enforce whatever it returns through the same state machine.
"""

from sqlalchemy.orm import Session

from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import routing, workflow
from app.initiatives.i7.recommendations.types import (
    ApprovalAction,
    ApprovalRole,
    LifecycleStatus,
    WorkflowError,
)
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation


def submit_for_approval(
    session: Session,
    recommendation: Recommendation,
    actor_id: str,
    policy: PolicyDocument | None = None,
) -> None:
    """Enter the approval chain. Requires READY_FOR_REVIEW."""
    policy = policy or PolicyDocument()
    route = routing.route_for(recommendation.is_oar, recommendation.criticality, policy)
    state = workflow.WorkflowState(
        status=LifecycleStatus(recommendation.status),
        chain_index=recommendation.chain_index,
        adjustment_count=recommendation.adjustment_count,
        route=route,
    )
    new_state = workflow.submit(state)

    session.add(
        ApprovalLedgerEntry(
            recommendation_id=recommendation.recommendation_id,
            recommendation_version=recommendation.current_version,
            actor_id=actor_id,
            actor_role=route[0].value,
            action="SUBMIT",
            previous_status=state.status.value,
            new_status=new_state.status.value,
            comment=None,
            timestamp=workflow.event_timestamp(),
        )
    )
    recommendation.status = new_state.status.value
    recommendation.chain_index = new_state.chain_index


def apply_approval_action(
    session: Session,
    recommendation: Recommendation,
    actor_id: str,
    actor_role: ApprovalRole,
    action: ApprovalAction,
    comment: str | None,
    policy: PolicyDocument | None = None,
) -> None:
    """Validate and apply one approval action, writing the ledger entry.

    Raises :class:`WorkflowError` before touching the database if the action is
    invalid -- an invalid action must never produce a partial write.
    """
    policy = policy or PolicyDocument()
    route = routing.route_for(recommendation.is_oar, recommendation.criticality, policy)
    state = workflow.WorkflowState(
        status=LifecycleStatus(recommendation.status),
        chain_index=recommendation.chain_index,
        adjustment_count=recommendation.adjustment_count,
        route=route,
    )
    new_state = workflow.apply_action(state, action, actor_role, comment)

    session.add(
        ApprovalLedgerEntry(
            recommendation_id=recommendation.recommendation_id,
            recommendation_version=recommendation.current_version,
            actor_id=actor_id,
            actor_role=actor_role.value,
            action=action.value,
            previous_status=state.status.value,
            new_status=new_state.status.value,
            comment=comment,
            timestamp=workflow.event_timestamp(),
        )
    )

    recommendation.status = new_state.status.value
    recommendation.chain_index = new_state.chain_index
    recommendation.adjustment_count = new_state.adjustment_count
    if action is ApprovalAction.ADJUST:
        recommendation.current_version += 1

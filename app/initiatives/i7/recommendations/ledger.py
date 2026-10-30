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


NOT_ELIGIBLE = "NOT_ELIGIBLE"
"""``conversion_eligibility`` of an OAR material the rule says stays on OAR ordering."""

SUBMIT = "SUBMIT"
RESUBMIT = "RESUBMIT"
"""The ledger actions for entering the chain, and for entering it again after a rejection."""


def is_manual_conversion(recommendation: Recommendation) -> bool:
    """A submission of this recommendation is a manual Min-Max conversion request.

    True for a material the rule put NOT_ELIGIBLE: sending it into the chain is the user overriding the
    rule's "no". The verdict itself is never changed -- only ``manually_converted``, ``requested_by`` and
    ``requested_at`` record that somebody asked.
    """
    return recommendation.conversion_eligibility == NOT_ELIGIBLE


def _has_recommended_values(recommendation: Recommendation) -> bool:
    return None not in (
        recommendation.recommended_rop,
        recommendation.recommended_safety_stock,
        recommendation.recommended_max_stock,
    )


def submit_for_approval(
    session: Session,
    recommendation: Recommendation,
    actor_id: str,
    policy: PolicyDocument | None = None,
) -> None:
    """Enter the approval chain. Requires READY_FOR_REVIEW -- or, for a manual conversion that was
    rejected, REJECTED (a new request).

    A NOT_ELIGIBLE material is a *manual conversion request*: ``manually_converted`` is set with who asked
    and when, and the rule's verdict is left as it was. It needs all three recommended values, because the
    approver has to see them. After a rejection it may be asked for again: the chain restarts at the first
    role, the earlier pass stays in the ledger, and a new entry (``RESUBMIT``) records the new request.
    Every other recommendation keeps REJECTED as final.
    """
    policy = policy or PolicyDocument()
    route = routing.route_for(recommendation.is_oar, recommendation.criticality, policy)
    state = workflow.WorkflowState(
        status=LifecycleStatus(recommendation.status),
        chain_index=recommendation.chain_index,
        adjustment_count=recommendation.adjustment_count,
        route=route,
    )
    manual = is_manual_conversion(recommendation)
    reopening = manual and state.status is LifecycleStatus.REJECTED
    new_state = workflow.resubmit(state) if reopening else workflow.submit(state)
    if manual and not _has_recommended_values(recommendation):
        raise WorkflowError(
            "cannot request a Min-Max conversion without recommended reorder point, safety stock and "
            "maximum stock for the approver to review"
        )

    now = workflow.event_timestamp()
    session.add(
        ApprovalLedgerEntry(
            recommendation_id=recommendation.recommendation_id,
            recommendation_version=recommendation.current_version,
            actor_id=actor_id,
            actor_role=route[0].value,
            action=RESUBMIT if reopening else SUBMIT,
            previous_status=state.status.value,
            new_status=new_state.status.value,
            comment=None,
            timestamp=now,
        )
    )
    recommendation.status = new_state.status.value
    recommendation.chain_index = new_state.chain_index
    if manual:
        recommendation.manually_converted = True
        recommendation.requested_by = actor_id
        recommendation.requested_at = now


def reject_before_submission(
    session: Session,
    recommendation: Recommendation,
    actor_id: str,
    comment: str | None,
    policy: PolicyDocument | None = None,
) -> None:
    """Decline a READY_FOR_REVIEW recommendation without submitting it, writing the ledger entry.

    The entry is a REJECT recorded under the route's first role -- the same role a SUBMIT is recorded
    under -- so the history reads as "declined at the point it would have been sent".
    """
    policy = policy or PolicyDocument()
    route = routing.route_for(recommendation.is_oar, recommendation.criticality, policy)
    state = workflow.WorkflowState(
        status=LifecycleStatus(recommendation.status),
        chain_index=recommendation.chain_index,
        adjustment_count=recommendation.adjustment_count,
        route=route,
    )
    new_state = workflow.reject_unsubmitted(state, comment)

    session.add(
        ApprovalLedgerEntry(
            recommendation_id=recommendation.recommendation_id,
            recommendation_version=recommendation.current_version,
            actor_id=actor_id,
            actor_role=route[0].value,
            action=ApprovalAction.REJECT.value,
            previous_status=state.status.value,
            new_status=new_state.status.value,
            comment=comment,
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

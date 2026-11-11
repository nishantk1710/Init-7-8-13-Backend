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

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import routing, workflow
from app.initiatives.i7.recommendations.types import (
    ApprovalAction,
    ApprovalRole,
    LifecycleStatus,
    WorkflowError,
)
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation, RecommendationVersion


NOT_ELIGIBLE = "NOT_ELIGIBLE"
"""``conversion_eligibility`` of an OAR material the rule says stays on OAR ordering."""

SUBMIT = "SUBMIT"
RESUBMIT = "RESUBMIT"
"""The ledger actions for entering the chain, and for entering it again after a rejection."""


LEDGER_COMMENT_MAX = 1000
"""The ``i7_approval_ledger.comment`` column length; an adjustment's summary is added to the comment."""


@dataclass(frozen=True)
class AdjustedValues:
    """What an ADJUST puts in place of the recommended values."""

    rop: int
    safety_stock: int
    max_stock: int


def _whole(value: Decimal | None) -> str:
    """A quantity for the summary: ``27`` for 27.000, ``-`` when there was none."""
    if value is None:
        return "-"
    return str(int(value)) if value == value.to_integral_value() else format(value, "f")


def _validate_adjustment(recommendation: Recommendation, values: AdjustedValues | None) -> AdjustedValues:
    if values is None:
        raise WorkflowError("ADJUST needs the new reorder point, safety stock and maximum stock")
    if min(values.rop, values.safety_stock, values.max_stock) < 0:
        raise WorkflowError("adjusted values cannot be negative")
    if not values.safety_stock <= values.rop <= values.max_stock:
        raise WorkflowError("adjusted values must satisfy safety stock <= reorder point <= maximum stock")
    current = (
        recommendation.recommended_rop,
        recommendation.recommended_safety_stock,
        recommendation.recommended_max_stock,
    )
    if all(c is not None and c == v for c, v in zip(current, (values.rop, values.safety_stock, values.max_stock))):
        raise WorkflowError("the adjusted values are the same as the current ones; change at least one")
    return values


def _adjustment_summary(recommendation: Recommendation, values: AdjustedValues) -> str:
    return (
        f"[ROP {_whole(recommendation.recommended_rop)} → {values.rop}, "
        f"Safety stock {_whole(recommendation.recommended_safety_stock)} → {values.safety_stock}, "
        f"Max {_whole(recommendation.recommended_max_stock)} → {values.max_stock}]"
    )


def _apply_adjustment(
    session: Session, recommendation: Recommendation, values: AdjustedValues, actor_id: str, comment: str
) -> None:
    """Keep the values being replaced as an immutable snapshot, then put the new ones in place.

    ``current_version`` moves on. The deltas are the new values against what SAP holds now, as they were
    for the generated ones. Adoption reads ``recommended_*``, so it checks SAP against the adjusted values.
    """
    newest_snapshot = session.execute(
        select(func.max(RecommendationVersion.version)).where(
            RecommendationVersion.recommendation_id == recommendation.recommendation_id
        )
    ).scalar()
    # recommendation_id is a label shared by regenerated rows, so a snapshot of an earlier row may already
    # hold this version number; take the next free one rather than collide.
    snapshot_version = max(recommendation.current_version, (newest_snapshot or 0) + 1)
    session.add(
        RecommendationVersion(
            recommendation_id=recommendation.recommendation_id,
            version=snapshot_version,
            recommended_safety_stock=recommendation.recommended_safety_stock,
            recommended_rop=recommendation.recommended_rop,
            recommended_max_stock=recommendation.recommended_max_stock,
            reason=comment[:500],
            changed_by=actor_id[:64],
        )
    )
    recommendation.recommended_rop = Decimal(values.rop)
    recommendation.recommended_safety_stock = Decimal(values.safety_stock)
    recommendation.recommended_max_stock = Decimal(values.max_stock)
    for delta, new, current in (
        ("rop_delta", values.rop, recommendation.current_rop),
        ("safety_stock_delta", values.safety_stock, recommendation.current_safety_stock),
        ("max_stock_delta", values.max_stock, recommendation.current_max_stock),
    ):
        setattr(recommendation, delta, None if current is None else Decimal(new) - current)
    recommendation.current_version = snapshot_version + 1


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
    adjusted: AdjustedValues | None = None,
) -> None:
    """Validate and apply one approval action, writing the ledger entry.

    Raises :class:`WorkflowError` before touching the database if the action is
    invalid -- an invalid action must never produce a partial write.

    ``adjusted`` is required for ADJUST (and ignored otherwise): the new values replace the recommended
    ones, the old ones are kept as a ``RecommendationVersion``, and the ledger comment carries the
    old -> new summary so the history and the notifications show what changed.
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

    if action is ApprovalAction.ADJUST:
        adjusted = _validate_adjustment(recommendation, adjusted)
        comment = f"{(comment or '').strip()} {_adjustment_summary(recommendation, adjusted)}"
        if len(comment) > LEDGER_COMMENT_MAX:
            raise WorkflowError(
                f"the comment is too long: with the adjustment summary it must fit {LEDGER_COMMENT_MAX} characters"
            )

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

    if action is ApprovalAction.ADJUST:
        _apply_adjustment(session, recommendation, adjusted, actor_id, comment)
    recommendation.status = new_state.status.value
    recommendation.chain_index = new_state.chain_index
    recommendation.adjustment_count = new_state.adjustment_count

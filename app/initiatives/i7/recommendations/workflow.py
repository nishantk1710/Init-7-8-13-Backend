"""The approval state machine, parameterised by route.

    Route[0] -> Route[1] -> ... -> Route[-1] -> SAP_EXECUTION_PENDING

Pure functions over an explicit state, so the rules are testable without a
database and the same logic runs whether Phase 8 calls it from a web request or
a batch job calls it from a script.

**The chain a recommendation follows is data, not a constant.** ROP/Max
recommendations are routed by criticality tier (see
``policy.ApprovalRoutingPolicy``); OAR conversion recommendations always use
``OAR_APPROVAL_CHAIN`` (Inventory Controller -> Commercial Head -> Engineering
Head -> Plant Head). Every function here takes the resolved ``route`` tuple
explicitly rather than reading a single module-level chain, so the same state
machine enforces both without a second implementation.

**A recommendation never reaches SAP.** APPROVE at the final role produces
``SAP_EXECUTION_PENDING`` and stops there -- there is no code path from any
action to a SAP write, because no such path exists to remove.

**ADJUST never mutates in place.** It records what changed and why, then
re-enters the chain at the point the adjustment was made from -- silently
editing an approved number is exactly the failure mode an audit trail exists to
catch.

**HOLD freezes; it never advances or reverses.** A HELD recommendation keeps
its exact ``chain_index`` -- the same pending role must RELEASE_HOLD it before
any other action becomes valid again. This is deliberately different from
SEND_BACK, which moves the pending role back one step because the material
substance of the recommendation is expected to change before re-review.
"""

from dataclasses import dataclass, replace
from datetime import datetime

from app.initiatives.i7.recommendations.types import (
    APPROVAL_CHAIN,
    ApprovalAction,
    ApprovalRole,
    LifecycleStatus,
    WorkflowError,
)

REQUIRES_COMMENT = frozenset(
    {ApprovalAction.REJECT, ApprovalAction.SEND_BACK, ApprovalAction.ADJUST, ApprovalAction.HOLD}
)
"""APPROVE and RELEASE_HOLD alone may proceed without a comment -- silence is
consent, and resuming a hold is a return to the status quo. Every other
action changes the outcome for someone else and must say why."""

_ACTIONABLE_STATUSES = (LifecycleStatus.PENDING_APPROVAL, LifecycleStatus.SENT_BACK)
"""Statuses from which the normal APPROVE/REJECT/SEND_BACK/ADJUST/HOLD actions
may be applied. HELD is deliberately excluded -- see ``apply_action``."""


@dataclass(frozen=True)
class WorkflowState:
    """Where one recommendation sits in a route.

    ``route`` defaults to the historical ROP/Max chain so existing callers
    that never pass one keep working unchanged.
    """

    status: LifecycleStatus
    chain_index: int
    """Index into ``route`` of the role whose decision is next pending.
    Meaningless once ``status`` leaves PENDING_APPROVAL/SENT_BACK/HELD."""

    adjustment_count: int = 0
    route: tuple[ApprovalRole, ...] = APPROVAL_CHAIN

    @property
    def pending_role(self) -> ApprovalRole | None:
        if self.status not in (
            LifecycleStatus.PENDING_APPROVAL,
            LifecycleStatus.SENT_BACK,
            LifecycleStatus.HELD,
        ):
            return None
        if self.chain_index >= len(self.route):
            return None
        return self.route[self.chain_index]


def start(
    state: LifecycleStatus = LifecycleStatus.READY_FOR_REVIEW,
    route: tuple[ApprovalRole, ...] = APPROVAL_CHAIN,
) -> WorkflowState:
    """A freshly-built recommendation, not yet submitted."""
    return WorkflowState(status=state, chain_index=0, route=route)


def submit(state: WorkflowState) -> WorkflowState:
    """Enter the chain at the first role.

    Only a READY_FOR_REVIEW recommendation may be submitted -- one that is
    NOT_EVALUABLE has no computed value for an approver to look at.
    """
    if state.status is not LifecycleStatus.READY_FOR_REVIEW:
        raise WorkflowError(
            f"cannot submit a recommendation in state {state.status.value}; "
            "only READY_FOR_REVIEW may enter the approval chain"
        )
    return replace(state, status=LifecycleStatus.PENDING_APPROVAL, chain_index=0)


def apply_action(
    state: WorkflowState,
    action: ApprovalAction,
    actor_role: ApprovalRole,
    comment: str | None,
) -> WorkflowState:
    """Validate and apply one approval action. Raises :class:`WorkflowError`.

    The state returned is the *new* state only -- the caller is responsible for
    writing the ledger entry alongside it, so the two never disagree about what
    happened.
    """
    if state.status is LifecycleStatus.HELD:
        if action is not ApprovalAction.RELEASE_HOLD:
            raise WorkflowError(
                f"{action.value} is not valid while HELD; only RELEASE_HOLD "
                "may act on a held recommendation"
            )
    elif state.status not in _ACTIONABLE_STATUSES:
        raise WorkflowError(
            f"no approval action is valid in state {state.status.value}"
        )
    elif action is ApprovalAction.RELEASE_HOLD:
        raise WorkflowError(
            f"RELEASE_HOLD is only valid from HELD, not {state.status.value}"
        )

    pending = state.pending_role
    if pending is None or pending != actor_role:
        raise WorkflowError(
            f"{actor_role.value} may not act; the pending role is "
            f"{pending.value if pending else 'none'}"
        )

    if action in REQUIRES_COMMENT and not (comment and comment.strip()):
        raise WorkflowError(f"{action.value} requires a non-empty comment")

    match action:
        case ApprovalAction.APPROVE:
            return _advance(state)
        case ApprovalAction.REJECT:
            return replace(state, status=LifecycleStatus.REJECTED)
        case ApprovalAction.SEND_BACK:
            # Returns to the role before the current one -- "review this
            # again" means the immediately preceding step, not the start of
            # the chain. Sent back from the first role has nowhere earlier to
            # go and stays there for correction.
            new_index = max(state.chain_index - 1, 0)
            return replace(state, status=LifecycleStatus.SENT_BACK, chain_index=new_index)
        case ApprovalAction.HOLD:
            # Freezes exactly where it is -- neither SEND_BACK's step back nor
            # APPROVE's step forward. Nothing else may act until the same
            # pending role releases it.
            return replace(state, status=LifecycleStatus.HELD)
        case ApprovalAction.RELEASE_HOLD:
            # Resumes at the identical position HOLD froze -- never a step
            # forward or back on its own.
            return replace(state, status=LifecycleStatus.PENDING_APPROVAL)
        case ApprovalAction.ADJUST:
            # Re-enters the chain from the role that made the adjustment,
            # since a later role must see the changed value before it proceeds
            # further -- adjustment count records that this happened at all.
            return replace(
                state,
                status=LifecycleStatus.PENDING_APPROVAL,
                adjustment_count=state.adjustment_count + 1,
            )
        case _:
            raise WorkflowError(f"unhandled action {action}")


def _advance(state: WorkflowState) -> WorkflowState:
    next_index = state.chain_index + 1
    if next_index >= len(state.route):
        return replace(
            state, status=LifecycleStatus.SAP_EXECUTION_PENDING, chain_index=next_index
        )
    return replace(state, status=LifecycleStatus.PENDING_APPROVAL, chain_index=next_index)


def event_timestamp() -> datetime:
    """A single seam for "now", so tests can freeze it without patching the
    standard library."""
    from datetime import timezone

    return datetime.now(timezone.utc)

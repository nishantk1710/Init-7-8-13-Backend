"""The four-step approval state machine.

    End User -> Engineering Manager -> Commercial Manager -> Warehouse Supervisor
        -> SAP_EXECUTION_PENDING

Pure functions over an explicit state, so the rules are testable without a
database and the same logic runs whether Phase 8 calls it from a web request or
a batch job calls it from a script.

**A recommendation never reaches SAP.** APPROVE at the final role produces
``SAP_EXECUTION_PENDING`` and stops there -- there is no code path from any
action to a SAP write, because no such path exists to remove.

**ADJUST never mutates in place.** It records what changed and why, then
re-enters the chain at the point the adjustment was made from -- silently
editing an approved number is exactly the failure mode an audit trail exists to
catch.
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
    {ApprovalAction.REJECT, ApprovalAction.SEND_BACK, ApprovalAction.ADJUST}
)
"""APPROVE alone may proceed without a comment -- silence is consent. Every
other action changes the outcome for someone else and must say why."""


@dataclass(frozen=True)
class WorkflowState:
    """Where one recommendation sits in the chain."""

    status: LifecycleStatus
    chain_index: int
    """Index into :data:`APPROVAL_CHAIN` of the role whose decision is next
    pending. Meaningless once ``status`` leaves PENDING_APPROVAL/SENT_BACK."""

    adjustment_count: int = 0

    @property
    def pending_role(self) -> ApprovalRole | None:
        if self.status not in (LifecycleStatus.PENDING_APPROVAL, LifecycleStatus.SENT_BACK):
            return None
        if self.chain_index >= len(APPROVAL_CHAIN):
            return None
        return APPROVAL_CHAIN[self.chain_index]


def start(state: LifecycleStatus = LifecycleStatus.READY_FOR_REVIEW) -> WorkflowState:
    """A freshly-built recommendation, not yet submitted."""
    return WorkflowState(status=state, chain_index=0)


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
    if state.status not in (LifecycleStatus.PENDING_APPROVAL, LifecycleStatus.SENT_BACK):
        raise WorkflowError(
            f"no approval action is valid in state {state.status.value}"
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
    if next_index >= len(APPROVAL_CHAIN):
        return replace(
            state, status=LifecycleStatus.SAP_EXECUTION_PENDING, chain_index=next_index
        )
    return replace(state, status=LifecycleStatus.PENDING_APPROVAL, chain_index=next_index)


def event_timestamp() -> datetime:
    """A single seam for "now", so tests can freeze it without patching the
    standard library."""
    from datetime import timezone

    return datetime.now(timezone.utc)

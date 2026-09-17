"""The four-step approval state machine.

Pure functions over an explicit state, so every rule is checked without a
database: the correct role can act, the wrong one cannot, stages cannot be
skipped, and the actions that change an outcome for someone else require a
reason.
"""

import pytest

from app.initiatives.i7.recommendations.types import (
    APPROVAL_CHAIN,
    ApprovalAction,
    ApprovalRole,
    LifecycleStatus,
    WorkflowError,
)
from app.initiatives.i7.recommendations.workflow import (
    WorkflowState,
    apply_action,
    start,
    submit,
)


def test_a_fresh_recommendation_starts_ready_for_review():
    state = start()
    assert state.status is LifecycleStatus.READY_FOR_REVIEW
    assert state.pending_role is None


def test_submitting_enters_the_first_role():
    state = submit(start())
    assert state.status is LifecycleStatus.PENDING_APPROVAL
    assert state.pending_role is ApprovalRole.END_USER


def test_not_evaluable_cannot_be_submitted():
    """A recommendation with no computed value must not enter the chain."""
    with pytest.raises(WorkflowError):
        submit(start(LifecycleStatus.NOT_EVALUABLE))


def test_the_correct_role_may_approve():
    state = submit(start())
    new_state = apply_action(state, ApprovalAction.APPROVE, ApprovalRole.END_USER, None)
    assert new_state.pending_role is ApprovalRole.ENGINEERING_MANAGER


def test_the_wrong_role_cannot_approve():
    state = submit(start())
    with pytest.raises(WorkflowError):
        apply_action(state, ApprovalAction.APPROVE, ApprovalRole.WAREHOUSE_SUPERVISOR, None)


def test_stages_cannot_be_skipped():
    """Approving as End User must not jump straight to Warehouse Supervisor."""
    state = submit(start())
    state = apply_action(state, ApprovalAction.APPROVE, ApprovalRole.END_USER, None)
    assert state.pending_role is ApprovalRole.ENGINEERING_MANAGER
    with pytest.raises(WorkflowError):
        apply_action(state, ApprovalAction.APPROVE, ApprovalRole.WAREHOUSE_SUPERVISOR, None)


def test_approving_through_the_full_chain_reaches_sap_execution_pending():
    state = submit(start())
    for role in APPROVAL_CHAIN:
        state = apply_action(state, ApprovalAction.APPROVE, role, None)
    assert state.status is LifecycleStatus.SAP_EXECUTION_PENDING


def test_final_approval_never_touches_sap():
    """The state machine's only output is a status. There is no SAP call in
    this module at all -- proven by inspecting what the module imports."""
    import inspect

    from app.initiatives.i7.recommendations import workflow

    source = inspect.getsource(workflow)
    for forbidden in ("requests", "http", "odata", "sap_client"):
        assert forbidden not in source.lower()


def test_reject_requires_a_comment():
    state = submit(start())
    with pytest.raises(WorkflowError):
        apply_action(state, ApprovalAction.REJECT, ApprovalRole.END_USER, None)
    with pytest.raises(WorkflowError):
        apply_action(state, ApprovalAction.REJECT, ApprovalRole.END_USER, "   ")


def test_reject_with_a_comment_succeeds():
    state = submit(start())
    new_state = apply_action(
        state, ApprovalAction.REJECT, ApprovalRole.END_USER, "not needed"
    )
    assert new_state.status is LifecycleStatus.REJECTED


def test_send_back_requires_a_comment():
    state = apply_action(
        submit(start()), ApprovalAction.APPROVE, ApprovalRole.END_USER, None
    )
    with pytest.raises(WorkflowError):
        apply_action(state, ApprovalAction.SEND_BACK, ApprovalRole.ENGINEERING_MANAGER, None)


def test_send_back_returns_to_the_prior_role():
    state = apply_action(
        submit(start()), ApprovalAction.APPROVE, ApprovalRole.END_USER, None
    )
    new_state = apply_action(
        state, ApprovalAction.SEND_BACK, ApprovalRole.ENGINEERING_MANAGER, "needs rework"
    )
    assert new_state.status is LifecycleStatus.SENT_BACK
    assert new_state.pending_role is ApprovalRole.END_USER


def test_send_back_from_the_first_role_stays_there():
    state = submit(start())
    new_state = apply_action(
        state, ApprovalAction.SEND_BACK, ApprovalRole.END_USER, "needs correction"
    )
    assert new_state.pending_role is ApprovalRole.END_USER


def test_adjust_requires_a_reason():
    state = submit(start())
    with pytest.raises(WorkflowError):
        apply_action(state, ApprovalAction.ADJUST, ApprovalRole.END_USER, None)


def test_adjust_records_the_adjustment_and_reenters_the_chain():
    state = submit(start())
    new_state = apply_action(
        state, ApprovalAction.ADJUST, ApprovalRole.END_USER, "corrected quantity"
    )
    assert new_state.adjustment_count == 1
    assert new_state.status is LifecycleStatus.PENDING_APPROVAL


def test_adjust_never_silently_changes_status_to_approved():
    """ADJUST is its own outcome, never mistaken for an approval."""
    state = submit(start())
    new_state = apply_action(
        state, ApprovalAction.ADJUST, ApprovalRole.END_USER, "corrected quantity"
    )
    assert new_state.status is not LifecycleStatus.APPROVED


def test_no_action_is_valid_after_rejection():
    state = apply_action(
        submit(start()), ApprovalAction.REJECT, ApprovalRole.END_USER, "no longer needed"
    )
    with pytest.raises(WorkflowError):
        apply_action(state, ApprovalAction.APPROVE, ApprovalRole.END_USER, None)


def test_no_action_is_valid_after_sap_execution_pending():
    state = submit(start())
    for role in APPROVAL_CHAIN:
        state = apply_action(state, ApprovalAction.APPROVE, role, None)
    with pytest.raises(WorkflowError):
        apply_action(state, ApprovalAction.APPROVE, ApprovalRole.WAREHOUSE_SUPERVISOR, None)


def test_chain_order_is_exactly_four_named_roles():
    assert APPROVAL_CHAIN == (
        ApprovalRole.END_USER,
        ApprovalRole.ENGINEERING_MANAGER,
        ApprovalRole.COMMERCIAL_MANAGER,
        ApprovalRole.WAREHOUSE_SUPERVISOR,
    )

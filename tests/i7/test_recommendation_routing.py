"""Approval routing: OAR's fixed 4-role chain, and criticality-based ROP/Max
routing via ApprovalRoutingPolicy.

The workflow state machine itself (workflow.py) is unchanged and untested
here -- these tests only cover which route a recommendation is given, not how
the state machine enforces whatever route it receives (see
test_recommendation_workflow.py for that).
"""

import pytest

from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.thresholds import ApprovalRoutingPolicy
from app.initiatives.i7.recommendations import routing, workflow
from app.initiatives.i7.recommendations.types import (
    APPROVAL_CHAIN,
    OAR_APPROVAL_CHAIN,
    ApprovalAction,
    ApprovalRole,
    LifecycleStatus,
    WorkflowError,
)


# --- OAR route: fixed, regardless of criticality ----------------------------


def test_oar_route_is_exactly_the_four_required_roles_in_order():
    assert OAR_APPROVAL_CHAIN == (
        ApprovalRole.INVENTORY_CONTROLLER,
        ApprovalRole.COMMERCIAL_HEAD,
        ApprovalRole.ENGINEERING_HEAD,
        ApprovalRole.PLANT_HEAD,
    )


@pytest.mark.parametrize("criticality", ["CRITICAL", "NORMAL", None, "IMPACT"])
def test_oar_recommendation_always_takes_the_oar_route_regardless_of_criticality(criticality):
    policy = PolicyDocument()
    route = routing.route_for(is_oar=True, criticality=criticality, policy=policy)
    assert route == OAR_APPROVAL_CHAIN


def test_oar_route_cannot_be_overridden_by_rop_max_routing_policy():
    """Even a policy that fully reconfigures the ROP/Max route must not
    affect OAR -- OAR always takes OAR_APPROVAL_CHAIN."""
    policy = PolicyDocument(
        approval_routing=ApprovalRoutingPolicy(
            rop_max_route_by_tier={"CRITICAL": ("End User",)}
        )
    )
    route = routing.route_for(is_oar=True, criticality="CRITICAL", policy=policy)
    assert route == OAR_APPROVAL_CHAIN


# --- ROP/Max route: criticality-configured, default otherwise --------------


def test_rop_max_route_defaults_to_the_historical_four_step_chain():
    """Unconfigured deployment: behaves exactly as before this policy
    existed."""
    policy = PolicyDocument()
    route = routing.route_for(is_oar=False, criticality="CRITICAL", policy=policy)
    assert route == APPROVAL_CHAIN


def test_rop_max_route_depends_on_criticality_when_configured():
    policy = PolicyDocument(
        approval_routing=ApprovalRoutingPolicy(
            rop_max_route_by_tier={
                "CRITICAL": ("Warehouse Supervisor",),
                "NORMAL": ("End User", "Engineering Manager"),
            }
        )
    )
    critical_route = routing.route_for(is_oar=False, criticality="CRITICAL", policy=policy)
    normal_route = routing.route_for(is_oar=False, criticality="NORMAL", policy=policy)
    unconfigured_tier_route = routing.route_for(is_oar=False, criticality="IMPACT", policy=policy)

    assert critical_route == (ApprovalRole.WAREHOUSE_SUPERVISOR,)
    assert normal_route == (ApprovalRole.END_USER, ApprovalRole.ENGINEERING_MANAGER)
    # A tier absent from the mapping falls back to default_route, not to an
    # empty or arbitrary route.
    assert unconfigured_tier_route == APPROVAL_CHAIN


def test_rop_max_route_with_no_criticality_uses_default_route():
    policy = PolicyDocument(
        approval_routing=ApprovalRoutingPolicy(
            rop_max_route_by_tier={"CRITICAL": ("End User",)}
        )
    )
    route = routing.route_for(is_oar=False, criticality=None, policy=policy)
    assert route == APPROVAL_CHAIN


def test_misconfigured_route_value_falls_back_to_the_default_route():
    """A typo in policy configuration (an unrecognised role string) must not
    silently produce a route nobody can act on -- fall back to the safe
    default rather than raise or return an empty route."""
    policy = PolicyDocument(
        approval_routing=ApprovalRoutingPolicy(
            rop_max_route_by_tier={"CRITICAL": ("Not A Real Role",)}
        )
    )
    route = routing.route_for(is_oar=False, criticality="CRITICAL", policy=policy)
    assert route == APPROVAL_CHAIN


def test_approval_routing_policy_is_read_from_policy_document_not_hardcoded():
    """Confirms the routing decision genuinely comes from configuration, by
    changing only the policy and observing the route change -- no hardcoded
    branch in routing.py keys off criticality itself."""
    default_policy = PolicyDocument()
    custom_policy = PolicyDocument(
        approval_routing=ApprovalRoutingPolicy(
            default_route=("Commercial Manager",)
        )
    )
    assert routing.route_for(False, "CRITICAL", default_policy) != routing.route_for(
        False, "CRITICAL", custom_policy
    )
    assert routing.route_for(False, "CRITICAL", custom_policy) == (
        ApprovalRole.COMMERCIAL_MANAGER,
    )


# --- OAR route enforced through the workflow state machine: no skipping,
# --- no auto-advance, every step human-gated ---------------------------


def _submitted_oar_state() -> workflow.WorkflowState:
    return workflow.submit(workflow.start(route=OAR_APPROVAL_CHAIN))


def test_oar_route_steps_cannot_be_skipped():
    state = _submitted_oar_state()
    assert state.pending_role is ApprovalRole.INVENTORY_CONTROLLER
    with pytest.raises(WorkflowError):
        workflow.apply_action(state, ApprovalAction.APPROVE, ApprovalRole.PLANT_HEAD, None)


def test_oar_route_requires_every_step_in_order_with_no_automatic_advance():
    state = _submitted_oar_state()
    for role in OAR_APPROVAL_CHAIN:
        assert state.pending_role is role
        assert state.status is LifecycleStatus.PENDING_APPROVAL
        state = workflow.apply_action(state, ApprovalAction.APPROVE, role, None)
    assert state.status is LifecycleStatus.SAP_EXECUTION_PENDING


def test_oar_route_rejects_an_invalid_acting_role():
    state = _submitted_oar_state()
    with pytest.raises(WorkflowError):
        workflow.apply_action(
            state, ApprovalAction.APPROVE, ApprovalRole.END_USER, None
        )


def test_oar_final_approval_never_reaches_sap():
    """No approval action, including the final OAR step, ever produces a
    status that implies a SAP call -- only SAP_EXECUTION_PENDING, which I07
    never acts on."""
    state = _submitted_oar_state()
    for role in OAR_APPROVAL_CHAIN:
        state = workflow.apply_action(state, ApprovalAction.APPROVE, role, None)
    assert state.status is LifecycleStatus.SAP_EXECUTION_PENDING
    assert state.status is not LifecycleStatus.SAP_EXECUTED

    import inspect

    source = inspect.getsource(workflow)
    for forbidden in ("requests", "http", "odata", "sap_client"):
        assert forbidden not in source.lower()


def test_oar_route_can_be_held_and_released_like_any_other_route():
    """HOLD/RELEASE_HOLD apply identically regardless of which route is in
    use -- the state machine does not special-case OAR."""
    state = workflow.apply_action(
        _submitted_oar_state(), ApprovalAction.APPROVE, ApprovalRole.INVENTORY_CONTROLLER, None
    )
    held = workflow.apply_action(
        state, ApprovalAction.HOLD, ApprovalRole.COMMERCIAL_HEAD, "awaiting budget review"
    )
    assert held.status is LifecycleStatus.HELD
    assert held.pending_role is ApprovalRole.COMMERCIAL_HEAD
    released = workflow.apply_action(
        held, ApprovalAction.RELEASE_HOLD, ApprovalRole.COMMERCIAL_HEAD, None
    )
    assert released.pending_role is ApprovalRole.COMMERCIAL_HEAD
    assert released.status is LifecycleStatus.PENDING_APPROVAL

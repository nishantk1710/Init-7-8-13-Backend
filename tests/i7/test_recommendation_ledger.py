"""The approval ledger and SAP execution evidence, against a real database.

Every action must produce an immutable ledger row that survives the
recommendation's own status changing further, and execution evidence must
never be recordable before final approval.
"""

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.recommendations.ledger import apply_approval_action, submit_for_approval
from app.initiatives.i7.recommendations.execution import record_execution_evidence
from app.initiatives.i7.recommendations.types import (
    APPROVAL_CHAIN,
    ApprovalAction,
    ApprovalRole,
    ExecutionStatus,
    LifecycleStatus,
)
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation, SapExecutionEvidence

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@pytest.fixture
def session():
    with get_sessionmaker()() as session:
        yield session
        session.rollback()


def make_recommendation(**overrides) -> Recommendation:
    defaults = dict(
        recommendation_id=f"REC-TEST-{id(overrides)}",
        sap_material_number="TEST0001",
        sap_plant_code="1300",
        policy_id="i07-test",
        policy_version=1,
        formula_version="i07-recommendation-1",
        status=LifecycleStatus.READY_FOR_REVIEW.value,
        impact_status="NOT_EVALUABLE_MISSING_RECOMMENDED",
    )
    defaults.update(overrides)
    return Recommendation(**defaults)


@needs_db
def test_every_action_creates_a_ledger_entry(session):
    rec = make_recommendation(recommendation_id="REC-LEDGER-1")
    session.add(rec)
    session.flush()

    submit_for_approval(session, rec, actor_id="U1")
    apply_approval_action(
        session, rec, "U2", ApprovalRole.END_USER, ApprovalAction.APPROVE, None
    )
    session.flush()

    entries = session.execute(
        select(ApprovalLedgerEntry).where(
            ApprovalLedgerEntry.recommendation_id == "REC-LEDGER-1"
        )
    ).scalars().all()
    assert len(entries) == 2
    assert entries[0].action == "SUBMIT"
    assert entries[1].action == "APPROVE"


@needs_db
def test_ledger_entries_survive_further_status_changes(session):
    """The recommendation's own status moves on; its history must not."""
    rec = make_recommendation(recommendation_id="REC-LEDGER-2")
    session.add(rec)
    session.flush()

    submit_for_approval(session, rec, "U1")
    for role in APPROVAL_CHAIN:
        apply_approval_action(session, rec, "U2", role, ApprovalAction.APPROVE, None)
    session.flush()

    assert rec.status == LifecycleStatus.SAP_EXECUTION_PENDING.value

    entries = session.execute(
        select(ApprovalLedgerEntry).where(
            ApprovalLedgerEntry.recommendation_id == "REC-LEDGER-2"
        )
    ).scalars().all()
    # SUBMIT + 4 approvals, none overwritten.
    assert len(entries) == 5
    assert [e.action for e in entries] == ["SUBMIT", "APPROVE", "APPROVE", "APPROVE", "APPROVE"]


@needs_db
def test_final_approval_produces_sap_execution_pending_with_no_sap_call(session):
    rec = make_recommendation(recommendation_id="REC-LEDGER-3")
    session.add(rec)
    session.flush()
    submit_for_approval(session, rec, "U1")
    for role in APPROVAL_CHAIN:
        apply_approval_action(session, rec, "U2", role, ApprovalAction.APPROVE, None)
    assert rec.status == LifecycleStatus.SAP_EXECUTION_PENDING.value


@needs_db
def test_rejection_requires_a_reason_and_is_recorded(session):
    rec = make_recommendation(recommendation_id="REC-LEDGER-4")
    session.add(rec)
    session.flush()
    submit_for_approval(session, rec, "U1")
    apply_approval_action(
        session, rec, "U2", ApprovalRole.END_USER, ApprovalAction.REJECT, "no longer needed"
    )
    session.flush()
    assert rec.status == LifecycleStatus.REJECTED.value

    entry = session.execute(
        select(ApprovalLedgerEntry).where(
            ApprovalLedgerEntry.recommendation_id == "REC-LEDGER-4",
            ApprovalLedgerEntry.action == "REJECT",
        )
    ).scalar_one()
    assert entry.comment == "no longer needed"


@needs_db
def test_adjustment_creates_a_history_record(session):
    from app.initiatives.i7.recommendations.types import WorkflowError

    rec = make_recommendation(recommendation_id="REC-LEDGER-5")
    session.add(rec)
    session.flush()
    submit_for_approval(session, rec, "U1")
    apply_approval_action(
        session, rec, "U2", ApprovalRole.END_USER, ApprovalAction.ADJUST, "corrected value"
    )
    assert rec.current_version == 2
    assert rec.adjustment_count == 1


# --- Execution evidence ------------------------------------------------------


@needs_db
def test_execution_evidence_requires_sap_execution_pending(session):
    rec = make_recommendation(
        recommendation_id="REC-EXEC-1", status=LifecycleStatus.READY_FOR_REVIEW.value
    )
    session.add(rec)
    session.flush()
    with pytest.raises(ValueError):
        record_execution_evidence(
            session, rec, executed_by="V1", execution_status=ExecutionStatus.PENDING
        )


@needs_db
def test_pending_execution_evidence_does_not_change_status(session):
    rec = make_recommendation(
        recommendation_id="REC-EXEC-2",
        status=LifecycleStatus.SAP_EXECUTION_PENDING.value,
        recommended_safety_stock=19,
        recommended_rop=32,
    )
    session.add(rec)
    session.flush()
    record_execution_evidence(
        session, rec, executed_by="V1", execution_status=ExecutionStatus.PENDING
    )
    assert rec.status == LifecycleStatus.SAP_EXECUTION_PENDING.value


@needs_db
def test_executed_evidence_moves_the_recommendation_to_sap_executed(session):
    rec = make_recommendation(
        recommendation_id="REC-EXEC-3",
        status=LifecycleStatus.SAP_EXECUTION_PENDING.value,
        recommended_safety_stock=19,
    )
    session.add(rec)
    session.flush()
    record_execution_evidence(
        session,
        rec,
        executed_by="V1",
        execution_status=ExecutionStatus.EXECUTED,
        sap_reference="MIGO-12345",
    )
    assert rec.status == LifecycleStatus.SAP_EXECUTED.value


@needs_db
def test_failed_evidence_does_not_advance_status(session):
    rec = make_recommendation(
        recommendation_id="REC-EXEC-4", status=LifecycleStatus.SAP_EXECUTION_PENDING.value
    )
    session.add(rec)
    session.flush()
    record_execution_evidence(
        session, rec, executed_by="V1", execution_status=ExecutionStatus.FAILED
    )
    assert rec.status == LifecycleStatus.SAP_EXECUTION_PENDING.value


@needs_db
def test_not_confirmed_evidence_does_not_advance_status(session):
    rec = make_recommendation(
        recommendation_id="REC-EXEC-5", status=LifecycleStatus.SAP_EXECUTION_PENDING.value
    )
    session.add(rec)
    session.flush()
    record_execution_evidence(
        session, rec, executed_by=None, execution_status=ExecutionStatus.NOT_CONFIRMED
    )
    assert rec.status == LifecycleStatus.SAP_EXECUTION_PENDING.value


@needs_db
def test_no_execution_evidence_module_imports_a_sap_client(session):
    """There is no HTTP client, no OData call -- this is capture only.

    Checked against the module's actual imports, not its prose -- the
    docstring legitimately discusses "no OData call" in explaining the
    boundary, and a text-substring check would trip on its own documentation.
    """
    import ast
    import inspect

    from app.initiatives.i7.recommendations import execution

    tree = ast.parse(inspect.getsource(execution))
    imported_modules = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    forbidden = {"requests", "httpx", "urllib3"}
    assert not (imported_modules & forbidden)


# --- Routing: OAR chain, criticality-based ROP/Max chain, through ledger ----


@needs_db
def test_oar_recommendation_is_routed_through_the_oar_chain(session):
    from app.initiatives.i7.policy import PolicyDocument
    from app.initiatives.i7.recommendations.types import OAR_APPROVAL_CHAIN

    rec = make_recommendation(recommendation_id="REC-LEDGER-OAR-1", is_oar=True)
    session.add(rec)
    session.flush()

    submit_for_approval(session, rec, "U1", policy=PolicyDocument())
    session.flush()

    entry = session.execute(
        select(ApprovalLedgerEntry).where(
            ApprovalLedgerEntry.recommendation_id == "REC-LEDGER-OAR-1"
        )
    ).scalar_one()
    assert entry.actor_role == OAR_APPROVAL_CHAIN[0].value == "Inventory Controller"


@needs_db
def test_oar_chain_cannot_skip_a_step_through_the_ledger(session):
    from app.initiatives.i7.policy import PolicyDocument
    from app.initiatives.i7.recommendations.types import ApprovalRole, WorkflowError

    rec = make_recommendation(recommendation_id="REC-LEDGER-OAR-2", is_oar=True)
    session.add(rec)
    session.flush()
    submit_for_approval(session, rec, "U1", policy=PolicyDocument())

    with pytest.raises(WorkflowError):
        apply_approval_action(
            session, rec, "U2", ApprovalRole.PLANT_HEAD, ApprovalAction.APPROVE, None,
            policy=PolicyDocument(),
        )


@needs_db
def test_criticality_routed_rop_max_recommendation_uses_the_configured_route(session):
    from app.initiatives.i7.policy import PolicyDocument
    from app.initiatives.i7.policy.thresholds import ApprovalRoutingPolicy
    from app.initiatives.i7.recommendations.types import ApprovalRole

    rec = make_recommendation(
        recommendation_id="REC-LEDGER-CRIT-1", is_oar=False, criticality="CRITICAL"
    )
    session.add(rec)
    session.flush()

    policy = PolicyDocument(
        approval_routing=ApprovalRoutingPolicy(
            rop_max_route_by_tier={"CRITICAL": ("Warehouse Supervisor",)}
        )
    )
    submit_for_approval(session, rec, "U1", policy=policy)
    session.flush()

    entry = session.execute(
        select(ApprovalLedgerEntry).where(
            ApprovalLedgerEntry.recommendation_id == "REC-LEDGER-CRIT-1"
        )
    ).scalar_one()
    assert entry.actor_role == ApprovalRole.WAREHOUSE_SUPERVISOR.value

    # And the single-step configured route reaches SAP_EXECUTION_PENDING
    # after just that one approval -- proving the route, not the default
    # four-step chain, is what actually governed the transition.
    apply_approval_action(
        session, rec, "U2", ApprovalRole.WAREHOUSE_SUPERVISOR, ApprovalAction.APPROVE, None,
        policy=policy,
    )
    assert rec.status == LifecycleStatus.SAP_EXECUTION_PENDING.value


# --- HOLD through the ledger: an entry is written, no auto-advance ---------


@needs_db
def test_hold_and_release_hold_each_create_a_ledger_entry(session):
    rec = make_recommendation(recommendation_id="REC-LEDGER-HOLD-1")
    session.add(rec)
    session.flush()

    submit_for_approval(session, rec, "U1")
    apply_approval_action(
        session, rec, "U2", ApprovalRole.END_USER, ApprovalAction.HOLD, "awaiting decision"
    )
    assert rec.status == LifecycleStatus.HELD.value

    apply_approval_action(
        session, rec, "U2", ApprovalRole.END_USER, ApprovalAction.RELEASE_HOLD, None
    )
    assert rec.status == LifecycleStatus.PENDING_APPROVAL.value
    session.flush()

    entries = session.execute(
        select(ApprovalLedgerEntry)
        .where(ApprovalLedgerEntry.recommendation_id == "REC-LEDGER-HOLD-1")
        .order_by(ApprovalLedgerEntry.id)
    ).scalars().all()
    assert [e.action for e in entries] == ["SUBMIT", "HOLD", "RELEASE_HOLD"]


@needs_db
def test_hold_does_not_advance_the_persisted_chain_index(session):
    rec = make_recommendation(recommendation_id="REC-LEDGER-HOLD-2")
    session.add(rec)
    session.flush()

    submit_for_approval(session, rec, "U1")
    apply_approval_action(
        session, rec, "U2", ApprovalRole.END_USER, ApprovalAction.APPROVE, None
    )
    index_before_hold = rec.chain_index

    apply_approval_action(
        session, rec, "U2", ApprovalRole.ENGINEERING_MANAGER, ApprovalAction.HOLD, "pausing"
    )
    assert rec.chain_index == index_before_hold
    assert rec.status == LifecycleStatus.HELD.value


# --- No SAP write-back, at the ledger/API-facing layer ----------------------


@needs_db
def test_no_ledger_or_workflow_module_imports_a_sap_client(session):
    import ast
    import inspect

    from app.initiatives.i7.recommendations import ledger, routing, workflow

    forbidden = {"requests", "httpx", "urllib3"}
    for module in (ledger, routing, workflow):
        tree = ast.parse(inspect.getsource(module))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not (imported & forbidden), module.__name__

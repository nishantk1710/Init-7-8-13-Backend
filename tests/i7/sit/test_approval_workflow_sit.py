"""SIT 5.5 / 5.6 / 5.11 / 5.14 -- the complete four-role workflow, negative
cases, ledger integrity, and retry safety, exercised through the real
Phase 7 ``workflow``/``ledger`` services -- never by writing a status directly.

TEST FIXTURE NOTE: no recommendation on the real database is READY_FOR_REVIEW
today (the service-level matrix is unsigned -- see test_recommendation_gating.py).
Exercising the workflow therefore requires one isolated, clearly-labelled
fixture recommendation (``REC-SIT-WORKFLOW-*``). Its ``recommended_safety_stock``
/``recommended_rop`` values are minimal synthetic numbers used only to satisfy
the "a value exists" precondition the real Phase 5/7 gate checks -- they are
never fed through a formula here, never treated as a real business answer, and
removed by the fixture teardown. They do not touch production seed data.
"""

import pytest
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.recommendations import ledger, workflow
from app.initiatives.i7.recommendations.types import (
    APPROVAL_CHAIN,
    ApprovalAction,
    ApprovalRole,
    WorkflowError,
)
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@pytest.fixture
def fixture_recommendation():
    """One isolated, test-only READY_FOR_REVIEW recommendation.

    Never seeded, never part of the pipeline's own generation -- removed in
    teardown regardless of test outcome.
    """
    recommendation_id = "REC-SIT-WORKFLOW-1"
    sf = get_sessionmaker()
    with sf() as session:
        session.execute(delete(ApprovalLedgerEntry).where(
            ApprovalLedgerEntry.recommendation_id == recommendation_id
        ))
        session.execute(delete(Recommendation).where(
            Recommendation.recommendation_id == recommendation_id
        ))
        session.add(
            Recommendation(
                recommendation_id=recommendation_id,
                sap_material_number="SITWORKFLOW01",
                sap_plant_code="1300",
                policy_id="i07-sit-fixture",
                policy_version=1,
                formula_version="i07-recommendation-2",
                status="READY_FOR_REVIEW",
                impact_status="NOT_EVALUABLE_MISSING_RECOMMENDED",
                recommended_safety_stock=7,
                recommended_rop=31,
            )
        )
        session.commit()

    yield recommendation_id

    with sf() as session:
        session.execute(delete(ApprovalLedgerEntry).where(
            ApprovalLedgerEntry.recommendation_id == recommendation_id
        ))
        session.execute(delete(Recommendation).where(
            Recommendation.recommendation_id == recommendation_id
        ))
        session.commit()


def _load(session, recommendation_id: str) -> Recommendation:
    return session.execute(
        select(Recommendation).where(Recommendation.recommendation_id == recommendation_id)
    ).scalar_one()


# --- The complete four-role chain ------------------------------------------------------------


@needs_db
def test_full_four_role_chain_reaches_sap_execution_pending(fixture_recommendation):
    rid = fixture_recommendation
    sf = get_sessionmaker()
    with sf() as session:
        row = _load(session, rid)
        ledger.submit_for_approval(session, row, "sit-end-user")
        session.commit()
        assert row.status == "PENDING_APPROVAL"

        for role in APPROVAL_CHAIN:
            row = _load(session, rid)
            ledger.apply_approval_action(
                session, row, f"sit-{role.value}", role, ApprovalAction.APPROVE, None
            )
            session.commit()

        row = _load(session, rid)
        assert row.status == "SAP_EXECUTION_PENDING"


# --- Negative cases ------------------------------------------------------------------------------


@needs_db
def test_wrong_actor_role_is_rejected(fixture_recommendation):
    rid = fixture_recommendation
    sf = get_sessionmaker()
    with sf() as session:
        row = _load(session, rid)
        ledger.submit_for_approval(session, row, "sit-user")
        session.commit()

        row = _load(session, rid)
        with pytest.raises(WorkflowError, match="may not act"):
            ledger.apply_approval_action(
                session, row, "sit-user", ApprovalRole.WAREHOUSE_SUPERVISOR,
                ApprovalAction.APPROVE, None,
            )


@needs_db
def test_invalid_transition_stage_skip_is_rejected(fixture_recommendation):
    rid = fixture_recommendation
    sf = get_sessionmaker()
    with sf() as session:
        row = _load(session, rid)
        ledger.submit_for_approval(session, row, "sit-user")
        ledger.apply_approval_action(
            session, row, "sit-user", ApprovalRole.END_USER, ApprovalAction.APPROVE, None
        )
        session.commit()

        row = _load(session, rid)
        with pytest.raises(WorkflowError):
            ledger.apply_approval_action(
                session, row, "sit-user", ApprovalRole.WAREHOUSE_SUPERVISOR,
                ApprovalAction.APPROVE, None,
            )


@needs_db
def test_reject_without_comment_is_rejected(fixture_recommendation):
    rid = fixture_recommendation
    sf = get_sessionmaker()
    with sf() as session:
        row = _load(session, rid)
        ledger.submit_for_approval(session, row, "sit-user")
        session.commit()

        row = _load(session, rid)
        with pytest.raises(WorkflowError, match="requires a non-empty comment"):
            ledger.apply_approval_action(
                session, row, "sit-user", ApprovalRole.END_USER, ApprovalAction.REJECT, None
            )


@needs_db
def test_reject_with_comment_terminates_the_recommendation(fixture_recommendation):
    rid = fixture_recommendation
    sf = get_sessionmaker()
    with sf() as session:
        row = _load(session, rid)
        ledger.submit_for_approval(session, row, "sit-user")
        ledger.apply_approval_action(
            session, row, "sit-user", ApprovalRole.END_USER, ApprovalAction.REJECT,
            "no longer needed",
        )
        session.commit()
        row = _load(session, rid)
        assert row.status == "REJECTED"


@needs_db
def test_send_back_requires_comment_and_returns_to_prior_role(fixture_recommendation):
    rid = fixture_recommendation
    sf = get_sessionmaker()
    with sf() as session:
        row = _load(session, rid)
        ledger.submit_for_approval(session, row, "sit-user")
        ledger.apply_approval_action(
            session, row, "sit-user", ApprovalRole.END_USER, ApprovalAction.APPROVE, None
        )
        session.commit()

        row = _load(session, rid)
        with pytest.raises(WorkflowError):
            ledger.apply_approval_action(
                session, row, "sit-user", ApprovalRole.ENGINEERING_MANAGER,
                ApprovalAction.SEND_BACK, None,
            )

        ledger.apply_approval_action(
            session, row, "sit-user", ApprovalRole.ENGINEERING_MANAGER,
            ApprovalAction.SEND_BACK, "needs rework",
        )
        session.commit()
        row = _load(session, rid)
        assert row.status == "SENT_BACK"

        state = workflow.WorkflowState(row.status, row.chain_index, row.adjustment_count)
        assert state.pending_role is ApprovalRole.END_USER


@needs_db
def test_adjust_requires_comment_and_increments_version(fixture_recommendation):
    rid = fixture_recommendation
    sf = get_sessionmaker()
    with sf() as session:
        row = _load(session, rid)
        ledger.submit_for_approval(session, row, "sit-user")
        session.commit()

        row = _load(session, rid)
        with pytest.raises(WorkflowError):
            ledger.apply_approval_action(
                session, row, "sit-user", ApprovalRole.END_USER, ApprovalAction.ADJUST, None
            )

        before_version = row.current_version
        ledger.apply_approval_action(
            session, row, "sit-user", ApprovalRole.END_USER, ApprovalAction.ADJUST,
            "corrected quantity",
        )
        session.commit()
        row = _load(session, rid)
        assert row.current_version == before_version + 1


# --- Repeated submission / repeated approval (retry safety) -------------------------------------


@needs_db
def test_repeated_submission_is_safely_rejected_not_duplicated(fixture_recommendation):
    """A retried SUBMIT must not create a second SUBMIT ledger entry -- the
    second call must be rejected by the real state machine, not silently
    absorbed as a no-op success."""
    rid = fixture_recommendation
    sf = get_sessionmaker()
    with sf() as session:
        row = _load(session, rid)
        ledger.submit_for_approval(session, row, "sit-user")
        session.commit()

        row = _load(session, rid)
        with pytest.raises(WorkflowError):
            ledger.submit_for_approval(session, row, "sit-user")

        entries = session.execute(
            select(ApprovalLedgerEntry).where(ApprovalLedgerEntry.recommendation_id == rid)
        ).scalars().all()
        assert [e.action for e in entries] == ["SUBMIT"]


@needs_db
def test_repeated_approve_at_the_same_role_is_safely_rejected(fixture_recommendation):
    """A duplicate/retried APPROVE at a role that has already acted must be
    rejected -- the pending role has already advanced -- never applied twice."""
    rid = fixture_recommendation
    sf = get_sessionmaker()
    with sf() as session:
        row = _load(session, rid)
        ledger.submit_for_approval(session, row, "sit-user")
        ledger.apply_approval_action(
            session, row, "sit-user", ApprovalRole.END_USER, ApprovalAction.APPROVE, None
        )
        session.commit()

        row = _load(session, rid)
        with pytest.raises(WorkflowError, match="may not act"):
            ledger.apply_approval_action(
                session, row, "sit-user", ApprovalRole.END_USER, ApprovalAction.APPROVE, None
            )

        entries = session.execute(
            select(ApprovalLedgerEntry).where(ApprovalLedgerEntry.recommendation_id == rid)
        ).scalars().all()
        assert [e.action for e in entries] == ["SUBMIT", "APPROVE"]


# --- 5.6: ledger integrity ---------------------------------------------------------------------


@needs_db
def test_ledger_preserves_every_field_in_order(fixture_recommendation):
    rid = fixture_recommendation
    sf = get_sessionmaker()
    with sf() as session:
        row = _load(session, rid)
        ledger.submit_for_approval(session, row, "actor-1")
        ledger.apply_approval_action(
            session, row, "actor-2", ApprovalRole.END_USER, ApprovalAction.APPROVE, None
        )
        ledger.apply_approval_action(
            session, row, "actor-3", ApprovalRole.ENGINEERING_MANAGER, ApprovalAction.REJECT,
            "budget denied",
        )
        session.commit()

        entries = session.execute(
            select(ApprovalLedgerEntry)
            .where(ApprovalLedgerEntry.recommendation_id == rid)
            .order_by(ApprovalLedgerEntry.timestamp, ApprovalLedgerEntry.id)
        ).scalars().all()

        assert [e.action for e in entries] == ["SUBMIT", "APPROVE", "REJECT"]
        assert entries[2].actor_id == "actor-3"
        assert entries[2].actor_role == "Engineering Manager"
        assert entries[2].comment == "budget denied"
        assert entries[2].previous_status == "PENDING_APPROVAL"
        assert entries[2].new_status == "REJECTED"
        assert all(e.recommendation_id == rid for e in entries)


@needs_db
def test_ledger_survives_the_recommendations_own_status_moving_on(fixture_recommendation):
    """Ledger rows are never overwritten or deleted as the recommendation
    itself progresses."""
    rid = fixture_recommendation
    sf = get_sessionmaker()
    with sf() as session:
        row = _load(session, rid)
        ledger.submit_for_approval(session, row, "actor-1")
        for role in APPROVAL_CHAIN:
            row = _load(session, rid)
            ledger.apply_approval_action(
                session, row, "actor-1", role, ApprovalAction.APPROVE, None
            )
        session.commit()

        entries = session.execute(
            select(ApprovalLedgerEntry).where(ApprovalLedgerEntry.recommendation_id == rid)
        ).scalars().all()
        assert len(entries) == 5  # SUBMIT + 4 APPROVE

"""SIT 5.2 / 5.4 / 5.15 -- the real database's blocked-state baseline, and the
gating invariants that must hold regardless of which upstream runs are live.

These assert the *expected* current state of the shared database rather than
building fresh data: 113,465 material-plants, 596 forecastable, 112,869
deferred to OAR, zero READY_FOR_REVIEW, because the service-level matrix is
unsigned. A future signed policy would change these numbers -- these tests
exist to prove the system reports that honestly when it happens, not to lock
the numbers in forever. Reported explicitly rather than hidden as invariants.

The baseline moved from 45,409 to 113,465 when the feature-builder's driving
query was fixed to union material-plant keys from MARC
(i7_staged_material_plant) and MARD (i7_staged_stock) instead of MARC alone --
Gamsberg (plant 1500) has zero MARC rows but is fully covered in MARD, and was
previously silently dropped from the feature store entirely. See
features/builder.py's _ATTRIBUTE_SQL for the fix.
"""

import pytest
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models.i7_features import MaterialFeature
from app.models.i7_recommendation import Recommendation

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@pytest.fixture
def session():
    with get_sessionmaker()() as session:
        yield session


def _latest_feature_run(session) -> int:
    return session.execute(text("select max(feature_run_id) from i7_recommendation")).scalar()


# --- 5.2: the documented baseline ------------------------------------------------------------


@needs_db
@pytest.mark.needs_seed_data
def test_feature_store_baseline_matches_the_documented_state(session):
    total = session.execute(select(func.count()).select_from(MaterialFeature)).scalar()
    assert total == 113465

    counts = dict(
        session.execute(
            text("select history_status, count(*) from i7_material_feature group by 1")
        ).all()
    )
    assert counts.get("SUFFICIENT") == 596
    assert counts.get("NO_HISTORY", 0) + counts.get("COLD_START", 0) == 112869


@needs_db
def test_recommendation_baseline_has_zero_ready_for_review(session):
    """The correct current outcome: every recommendation is blocked, because
    the service-level matrix is unsigned. This is not a defect -- it is the
    Phase 7 gate working as designed."""
    latest = _latest_feature_run(session)
    ready = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(Recommendation.feature_run_id == latest, Recommendation.status == "READY_FOR_REVIEW")
    ).scalar()
    assert ready == 0


@needs_db
@pytest.mark.needs_seed_data
def test_oar_similarity_available_but_estimate_blocked(session):
    """Every OAR target with an available similarity match is still blocked
    from a SUCCESS estimate -- similarity data alone is not enough to
    produce one.

    Two legitimate block reasons now exist, not one: the unsigned
    service-level matrix (NOT_EVALUABLE_SERVICE_LEVEL_UNSET, still the
    majority case) and, since the minimum_neighbours=5 /
    minimum_similarity=0.60 admission gate was added, targets whose
    similarity matched something but fewer than 5 candidates cleared the
    0.60 floor (NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS) -- 570 of the current
    baseline. Asserting "no AVAILABLE-similarity target reaches SUCCESS"
    (rather than pinning it to one specific reason) is the invariant that
    actually matters and survives future data/policy changes.
    """
    latest = _latest_feature_run(session)
    available = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            Recommendation.feature_run_id == latest,
            Recommendation.oar_similarity_status == "AVAILABLE",
        )
    ).scalar()
    assert available > 0

    blocked = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            Recommendation.feature_run_id == latest,
            Recommendation.oar_similarity_status == "AVAILABLE",
            Recommendation.oar_estimate_status.in_(
                (
                    "NOT_EVALUABLE_SERVICE_LEVEL_UNSET",
                    "NOT_EVALUABLE_NEIGHBOR_INVENTORY",
                    "NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS",
                )
            ),
        )
    ).scalar()
    assert blocked == available


@needs_db
def test_sap_adoption_evidence_is_unavailable_on_this_extract(session):
    """No staged CDHDR/CDPOS exists (Phase 2 never materialised them), so
    every adoption evaluation must resolve to UNKNOWN, never NOT_ADOPTED."""
    from app.initiatives.i7.recommendations.adoption import evaluate_parameter_adoption

    result = evaluate_parameter_adoption("1000000009", "1300", 7, 31, 29)
    assert result.status.value == "UNKNOWN"


# --- 5.4 / invariant 1-2-3: gating -----------------------------------------------------------


@needs_db
def test_invariant_ready_for_review_implies_recommended_values_exist(session):
    """Invariant 1: READY_FOR_REVIEW => required recommended values exist."""
    violations = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            Recommendation.status == "READY_FOR_REVIEW",
            (Recommendation.recommended_safety_stock.is_(None))
            | (Recommendation.recommended_rop.is_(None)),
        )
    ).scalar()
    assert violations == 0


@needs_db
def test_invariant_service_level_unset_implies_no_recommended_safety_stock(session):
    """Invariant 2: for the normal path, an unsigned service level must never
    coexist with a non-null recommended safety stock."""
    violations = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            Recommendation.is_oar.is_(False),
            Recommendation.recommended_safety_stock.isnot(None),
        )
    ).scalar()
    assert violations == 0


@needs_db
def test_invariant_oar_estimate_blocked_implies_not_ready_for_review(session):
    """Invariant 3: OAR estimate blocked => never READY_FOR_REVIEW, even when
    similarity itself is AVAILABLE."""
    violations = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            Recommendation.oar_similarity_status == "AVAILABLE",
            Recommendation.oar_estimate_status != "SUCCESS",
            Recommendation.status == "READY_FOR_REVIEW",
        )
    ).scalar()
    assert violations == 0


@needs_db
def test_invariant_recommended_values_are_null_wherever_blocked(session):
    """Invariant: NOT_EVALUABLE => recommended SS/ROP/Max are NULL, never a
    fabricated zero."""
    fabricated = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            Recommendation.status == "NOT_EVALUABLE",
            (
                (Recommendation.recommended_safety_stock.isnot(None))
                | (Recommendation.recommended_rop.isnot(None))
                | (Recommendation.recommended_max_stock.isnot(None))
            ),
        )
    ).scalar()
    assert fabricated == 0


@needs_db
def test_invariant_material_plant_identity_is_never_null(session):
    """Invariant 8: material + plant identity is always preserved."""
    missing = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            (Recommendation.sap_material_number.is_(None))
            | (Recommendation.sap_plant_code.is_(None))
        )
    ).scalar()
    assert missing == 0


# --- 5.15: rollback safety on an invalid attempt ----------------------------------------------


@needs_db
def test_a_rejected_workflow_action_leaves_no_partial_state(session):
    """An invalid transition must raise before any write happens -- the
    recommendation's row must be byte-for-byte unchanged, and no ledger row
    must appear."""
    from app.initiatives.i7.recommendations import ledger
    from app.initiatives.i7.recommendations.types import (
        ApprovalAction,
        ApprovalRole,
        WorkflowError,
    )
    from app.models.i7_recommendation import ApprovalLedgerEntry

    recommendation_id = "REC-SIT-ROLLBACK-1"
    session.execute(text("delete from i7_approval_ledger where recommendation_id = :r"), {"r": recommendation_id})
    session.execute(text("delete from i7_recommendation where recommendation_id = :r"), {"r": recommendation_id})
    session.commit()

    row = Recommendation(
        recommendation_id=recommendation_id,
        sap_material_number="SITROLLBACK",
        sap_plant_code="1300",
        policy_id="i07-test",
        policy_version=1,
        formula_version="i07-recommendation-2",
        status="NOT_EVALUABLE",
        impact_status="NOT_EVALUABLE_MISSING_RECOMMENDED",
    )
    session.add(row)
    session.commit()

    before_status = row.status
    before_chain = row.chain_index

    with pytest.raises(WorkflowError):
        ledger.apply_approval_action(
            session, row, "U1", ApprovalRole.END_USER, ApprovalAction.APPROVE, None
        )

    # apply_approval_action must have raised before mutating the row or
    # queuing a ledger insert -- confirmed by checking both are unchanged
    # without an intervening commit.
    assert row.status == before_status
    assert row.chain_index == before_chain
    ledger_rows = session.execute(
        text("select count(*) from i7_approval_ledger where recommendation_id = :r"),
        {"r": recommendation_id},
    ).scalar()
    assert ledger_rows == 0

    session.execute(text("delete from i7_recommendation where recommendation_id = :r"), {"r": recommendation_id})
    session.commit()

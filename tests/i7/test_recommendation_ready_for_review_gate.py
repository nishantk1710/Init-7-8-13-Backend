"""READY_FOR_REVIEW requires an actual computable value, not just evidence.

Regression coverage for the Phase 7 correction: the OAR builder previously
granted READY_FOR_REVIEW whenever Phase 6 found neighbours, even though the
weighted SS/ROP/Max estimate was still blocked on the unsigned service level.
Similarity availability and recommendation reviewability are different facts,
and only the second may produce READY_FOR_REVIEW.
"""

from decimal import Decimal
from types import SimpleNamespace

from app.initiatives.i7.recommendations import builder
from app.initiatives.i7.recommendations.types import LifecycleStatus
from app.initiatives.i7.policy import PolicyDocument, ServiceLevelKey, ServiceLevelPolicy
from app.initiatives.i7.contracts.enums import Criticality


def oar_row(**overrides) -> SimpleNamespace:
    defaults = dict(
        sap_material_number="4000000001",
        sap_plant_code="1300",
        history_status="NO_HISTORY",
        criticality=None,
        non_zero_periods=None,
        consumption_count_12m=None,
        demand_class="UNCLASSIFIED",
        feature_run_id=1,
        status="SUCCESS",
        confidence="LOW",
        candidates_considered=471,
        eligible_candidates=4,
        neighbour_count=4,
        best_similarity=Decimal("0.65"),
        estimate_status="NOT_EVALUABLE_SERVICE_LEVEL_UNSET",
        oar_safety_stock=None,
        oar_rop=None,
        oar_max_stock=None,
        oar_run_id=7,
        current_safety_stock=None,
        current_reorder_point=None,
        current_maximum_stock=None,
        unit_price=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


UNSIGNED_POLICY = PolicyDocument()

SIGNED_POLICY = PolicyDocument(
    service_level=ServiceLevelPolicy(
        matrix=tuple((ServiceLevelKey(criticality=tier), 0.95) for tier in Criticality)
    )
)


# --- A: neighbours available, service level unavailable -> NOT_EVALUABLE ------


def test_a_neighbours_available_service_level_unavailable_is_not_evaluable():
    row = oar_row(
        neighbour_count=4,
        estimate_status="NOT_EVALUABLE_SERVICE_LEVEL_UNSET",
        oar_safety_stock=None,
        oar_rop=None,
        oar_max_stock=None,
    )
    result = builder.build_oar_recommendation(row, UNSIGNED_POLICY)

    assert result.status is LifecycleStatus.NOT_EVALUABLE
    assert result.blocking_reason == "SERVICE_LEVEL_UNSET"
    # Similarity evidence must still be visible, not discarded.
    assert result.oar_similarity_status == "AVAILABLE"
    assert result.oar_neighbour_count == 4
    assert result.confidence == "LOW"
    assert result.oar_estimate_status == "NOT_EVALUABLE_SERVICE_LEVEL_UNSET"
    # No fabricated recommended values.
    assert result.recommended_safety_stock is None
    assert result.recommended_rop is None
    assert result.recommended_max_stock is None


# --- B: neighbours unavailable -> NOT_EVALUABLE -------------------------------


def test_b_no_eligible_neighbours_is_not_evaluable():
    row = oar_row(
        neighbour_count=0,
        eligible_candidates=0,
        best_similarity=None,
        status="NO_ELIGIBLE_NEIGHBORS",
        confidence="LOW",
        estimate_status="NOT_EVALUABLE_NO_NEIGHBORS",
    )
    result = builder.build_oar_recommendation(row, UNSIGNED_POLICY)

    assert result.status is LifecycleStatus.NOT_EVALUABLE
    assert result.oar_similarity_status == "NOT_AVAILABLE"
    assert result.recommended_safety_stock is None


# --- C: neighbours + complete SS/ROP/Max -> READY_FOR_REVIEW ------------------


def test_c_neighbours_with_complete_estimate_is_ready_for_review():
    row = oar_row(
        neighbour_count=5,
        estimate_status="SUCCESS",
        oar_safety_stock=15,
        oar_rop=30,
        oar_max_stock=45,
    )
    result = builder.build_oar_recommendation(row, SIGNED_POLICY)

    assert result.status is LifecycleStatus.READY_FOR_REVIEW
    assert result.recommended_safety_stock == 15
    assert result.recommended_rop == 30
    assert result.recommended_max_stock == 45
    assert result.oar_similarity_status == "AVAILABLE"
    assert result.oar_estimate_status == "SUCCESS"
    assert result.blocking_reason is None


# --- D: normal recommendation, service level unavailable -> NOT_EVALUABLE ------


def normal_row(**overrides) -> SimpleNamespace:
    defaults = dict(
        sap_material_number="1000000001",
        sap_plant_code="1300",
        demand_class="SMOOTH",
        history_status="SUFFICIENT",
        criticality=None,
        non_zero_periods=10,
        feature_run_id=1,
        baseline_model="SES",
        forecast_rate=Decimal("5.83"),
        current_safety_stock=Decimal(2),
        current_reorder_point=Decimal(4),
        current_maximum_stock=Decimal(6),
        unit_price=None,
        lead_time_method="ACTUAL_STATISTICAL",
        valid_po_count=6,
        lt_avg_months=Decimal("3.99"),
        lt_avg_days=Decimal("121.4"),
        sigma_lt_days=Decimal("12.0"),
        circuit=None,
        service_level=None,
        z_factor=None,
        safety_stock_status="NOT_EVALUABLE_SERVICE_LEVEL_UNSET",
        safety_stock_method=None,
        safety_stock=None,
        detail="the Criticality x Circuit service-level matrix must be supplied and signed",
        rop_status="NOT_EVALUABLE_SERVICE_LEVEL_UNSET",
        rop=None,
        max_stock_status="NOT_CONFIGURED",
        max_stock_strategy=None,
        max_stock=None,
        inventory_run_id=1,
        forecast_run_id=1,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_d_normal_recommendation_service_level_unavailable_is_not_evaluable():
    row = normal_row()
    result = builder.build_normal_recommendation(row, UNSIGNED_POLICY)

    assert result.status is LifecycleStatus.NOT_EVALUABLE
    assert result.recommended_safety_stock is None
    assert result.recommended_rop is None
    assert result.blocking_reason is not None


def test_d_normal_recommendation_lead_time_unavailable_is_not_evaluable():
    row = normal_row(
        lead_time_method=None,
        safety_stock_status="NOT_EVALUABLE_LEAD_TIME",
        rop_status="NOT_EVALUABLE_LEAD_TIME",
        detail="no usable lead time",
    )
    result = builder.build_normal_recommendation(row, SIGNED_POLICY)

    assert result.status is LifecycleStatus.NOT_EVALUABLE
    assert result.recommended_safety_stock is None


def test_d_normal_recommendation_with_success_is_ready_for_review():
    """The positive control: a genuinely complete Phase 5 result is
    reviewable."""
    row = normal_row(
        service_level=Decimal("0.95"),
        z_factor=Decimal("1.6449"),
        safety_stock_status="SUCCESS",
        safety_stock_method="normal",
        safety_stock=7,
        detail=None,
        rop_status="SUCCESS",
        rop=31,
    )
    result = builder.build_normal_recommendation(row, SIGNED_POLICY)

    assert result.status is LifecycleStatus.READY_FOR_REVIEW
    assert result.recommended_safety_stock == 7
    assert result.recommended_rop == 31


# --- E: READY_FOR_REVIEW cannot exist without actual recommended values ---------


def test_e_ready_for_review_always_carries_recommended_values():
    """Sweep a range of OAR and normal inputs; whenever the builder emits
    READY_FOR_REVIEW, at least the safety stock and ROP must be populated."""
    cases = [
        oar_row(neighbour_count=5, estimate_status="SUCCESS", oar_safety_stock=10,
                oar_rop=20, oar_max_stock=30),
        oar_row(neighbour_count=0, estimate_status="NOT_EVALUABLE_NO_NEIGHBORS"),
        oar_row(neighbour_count=3, estimate_status="NOT_EVALUABLE_SERVICE_LEVEL_UNSET"),
    ]
    for row in cases:
        result = builder.build_oar_recommendation(row, SIGNED_POLICY)
        if result.status is LifecycleStatus.READY_FOR_REVIEW:
            assert result.recommended_safety_stock is not None
            assert result.recommended_rop is not None

    normal_cases = [
        normal_row(safety_stock_status="SUCCESS", rop_status="SUCCESS", safety_stock=7, rop=31),
        normal_row(),  # blocked
    ]
    for row in normal_cases:
        result = builder.build_normal_recommendation(row, SIGNED_POLICY)
        if result.status is LifecycleStatus.READY_FOR_REVIEW:
            assert result.recommended_safety_stock is not None
            assert result.recommended_rop is not None


def test_e_no_ready_for_review_without_estimate_success_oar():
    """The bug this correction fixes, stated as its own explicit assertion:
    neighbours alone must never produce READY_FOR_REVIEW."""
    row = oar_row(
        neighbour_count=10,
        best_similarity=Decimal("0.95"),
        confidence="HIGH",
        estimate_status="NOT_EVALUABLE_SERVICE_LEVEL_UNSET",
    )
    result = builder.build_oar_recommendation(row, UNSIGNED_POLICY)
    assert result.status is not LifecycleStatus.READY_FOR_REVIEW

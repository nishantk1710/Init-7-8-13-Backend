"""FR-2 demand class exposed on OAR conversion recommendations as a
supporting regularity/confidence signal -- never an eligibility trigger.

The requirement: "the FR-2 demand class as a confidence signal" and "the ADI
/ CV squared class is shown as a supporting regularity and confidence signal,
not the trigger." No FR-2 classification logic changes here -- this only
fixes the propagation gap where ``build_oar_recommendation()`` hardcoded
``demand_class=None`` regardless of what ``MaterialFeature.demand_class``
actually held.
"""

from decimal import Decimal
from types import SimpleNamespace

from app.initiatives.i7.contracts.enums import Criticality
from app.initiatives.i7.policy import (
    PolicyDocument,
    ServiceLevelKey,
    ServiceLevelPolicy,
)
from app.initiatives.i7.recommendations import builder


def oar_row(**overrides) -> SimpleNamespace:
    defaults = dict(
        sap_material_number="4000000001",
        sap_plant_code="1300",
        history_status="COLD_START",
        criticality=None,
        non_zero_periods=None,
        consumption_count_12m=0,
        demand_class="UNCLASSIFIED",
        oar_scope="IN_SCOPE",
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
    ),
)


# --- Case 1: FR-2 classified it -- expose the real class --------------------


def test_classified_oar_material_exposes_its_demand_class():
    row = oar_row(demand_class="INTERMITTENT")
    result = builder.build_oar_recommendation(row, UNSIGNED_POLICY)
    assert result.demand_class == "INTERMITTENT"


# --- Case 2: FR-2 could not classify it -- expose UNCLASSIFIED, not None ----


def test_unclassified_oar_material_exposes_unclassified_not_none():
    row = oar_row(demand_class="UNCLASSIFIED")
    result = builder.build_oar_recommendation(row, UNSIGNED_POLICY)
    assert result.demand_class == "UNCLASSIFIED"
    assert result.demand_class is not None


def test_unclassified_is_never_upgraded_to_a_real_pattern():
    """No SMOOTH/ERRATIC/INTERMITTENT/LUMPY may be invented just to populate
    the field."""
    row = oar_row(demand_class="UNCLASSIFIED")
    result = builder.build_oar_recommendation(row, UNSIGNED_POLICY)
    assert result.demand_class not in ("SMOOTH", "ERRATIC", "INTERMITTENT", "LUMPY")


def test_existing_history_status_reason_is_still_exposed_alongside_it():
    """The existing FR-2 status/reason (history_status) is reused, not a new
    status model."""
    row = oar_row(demand_class="UNCLASSIFIED", history_status="NO_HISTORY")
    result = builder.build_oar_recommendation(row, UNSIGNED_POLICY)
    assert result.demand_class == "UNCLASSIFIED"
    assert result.history_status == "NO_HISTORY"


def test_rationale_carries_the_demand_class_as_a_supporting_signal():
    row = oar_row(demand_class="INTERMITTENT")
    result = builder.build_oar_recommendation(row, UNSIGNED_POLICY)
    # Said in words a non-technical reader follows, not as the class code.
    assert any("occasional" in factor.detail for factor in result.factors)
    assert any(factor.label == "Usage pattern" for factor in result.factors)


# --- Eligibility must remain completely independent of demand_class --------


def test_eligibility_holds_regardless_of_demand_class():
    """consumption_count_12m > 4 + Critical + demand_class = INTERMITTENT ->
    eligible on usage and criticality, not demand class."""
    row = oar_row(
        demand_class="INTERMITTENT",
        consumption_count_12m=7,
        criticality=Criticality.CRITICAL.value,
    )
    result = builder.build_oar_recommendation(row, UNSIGNED_POLICY)
    assert result.conversion.eligibility.value == "ELIGIBLE"
    assert result.conversion.trigger.value == "CONSUMPTION_AND_CRITICALITY"
    assert result.demand_class == "INTERMITTENT"


def test_critical_alone_is_eligible_with_unclassified_demand_class():
    """consumption_count_12m <= 4 + Critical + demand_class = UNCLASSIFIED ->
    eligible on criticality alone; demand class plays no part."""
    row = oar_row(
        demand_class="UNCLASSIFIED",
        consumption_count_12m=2,
        criticality=Criticality.CRITICAL.value,
    )
    result = builder.build_oar_recommendation(row, UNSIGNED_POLICY)
    assert result.conversion.eligibility.value == "ELIGIBLE"
    assert result.conversion.trigger.value == "PRODUCTION_IMPACT"
    assert result.demand_class == "UNCLASSIFIED"


def test_no_condition_holds_not_eligible_regardless_of_demand_class():
    """consumption_count_12m <= 4, Normal -> not eligible, whatever
    demand_class says."""
    row = oar_row(
        demand_class="LUMPY",
        consumption_count_12m=2,
        criticality=Criticality.NORMAL.value,
    )
    result = builder.build_oar_recommendation(row, SIGNED_POLICY)
    assert result.conversion.eligibility.value == "NOT_ELIGIBLE"
    assert result.demand_class == "LUMPY"


def test_smooth_demand_class_does_not_create_eligibility_on_its_own():
    """demand_class = SMOOTH must not make an otherwise ineligible OAR
    eligible."""
    row = oar_row(
        demand_class="SMOOTH",
        consumption_count_12m=1,
        criticality=Criticality.NORMAL.value,
    )
    result = builder.build_oar_recommendation(row, SIGNED_POLICY)
    assert result.conversion.eligibility.value == "NOT_ELIGIBLE"


def test_unclassified_demand_class_does_not_block_an_otherwise_eligible_oar():
    """demand_class = UNCLASSIFIED must not make an otherwise eligible OAR
    ineligible."""
    row = oar_row(
        demand_class="UNCLASSIFIED",
        consumption_count_12m=10,
        criticality=Criticality.IMPACT.value,
    )
    result = builder.build_oar_recommendation(row, UNSIGNED_POLICY)
    assert result.conversion.eligibility.value == "ELIGIBLE"
    assert result.conversion.trigger.value == "CONSUMPTION_AND_CRITICALITY"


def test_a_cold_start_material_stage_1_did_not_call_oar_gets_no_decision():
    """The OAR recommendation path is chosen by history, not MRP type. A
    material-plant Stage 1 put OUT_OF_SCOPE (e.g. a VB material with thin
    history) is not evaluated at all, however it would otherwise score -- a
    "not eligible" on it would read as "stays on OAR"."""
    row = oar_row(
        oar_scope="OUT_OF_SCOPE",
        consumption_count_12m=10,
        criticality=Criticality.CRITICAL.value,
    )
    result = builder.build_oar_recommendation(row, UNSIGNED_POLICY)
    assert result.conversion is None


# --- The standard path is evaluated too: OAR is MRP type, not history ----------


def normal_row(**overrides) -> SimpleNamespace:
    defaults = dict(
        sap_material_number="5000019221",
        sap_plant_code="1300",
        demand_class="INTERMITTENT",
        history_status="SUFFICIENT",
        criticality=Criticality.CRITICAL.value,
        non_zero_periods=7,
        consumption_count_12m=7,
        oar_scope="IN_SCOPE",
        feature_run_id=1,
        baseline_model="SBA",
        forecast_rate=Decimal("1.2"),
        current_safety_stock=Decimal(1),
        current_reorder_point=Decimal(2),
        current_maximum_stock=Decimal(4),
        unit_price=None,
        lead_time_method="PLANNED_FALLBACK",
        valid_po_count=0,
        lt_avg_months=Decimal("1"),
        lt_avg_days=Decimal("30"),
        sigma_lt_days=Decimal("3"),
        circuit=None,
        service_level=None,
        z_factor=None,
        safety_stock_status="NOT_EVALUABLE_SERVICE_LEVEL_UNSET",
        safety_stock_method=None,
        safety_stock=None,
        detail=None,
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


def test_a_standard_path_oar_material_is_evaluated():
    """The crusher liner case: frequently used, CRITICAL, PD/ND -- on the
    standard path because it has history, and still a conversion candidate."""
    result = builder.build_normal_recommendation(normal_row(), UNSIGNED_POLICY)
    assert result.is_oar is False
    assert result.conversion.eligibility.value == "ELIGIBLE"
    assert result.conversion.trigger.value == "CONSUMPTION_AND_CRITICALITY"


def test_a_standard_path_oar_material_can_be_not_eligible():
    row = normal_row(consumption_count_12m=2, criticality=Criticality.NORMAL.value)
    result = builder.build_normal_recommendation(row, UNSIGNED_POLICY)
    assert result.conversion.eligibility.value == "NOT_ELIGIBLE"


def test_a_standard_path_obsolete_material_is_not_eligible():
    row = normal_row(consumption_count_12m=40, criticality=Criticality.OBSOLETE.value)
    result = builder.build_normal_recommendation(row, UNSIGNED_POLICY)
    assert result.conversion.eligibility.value == "NOT_ELIGIBLE"


def test_a_standard_path_material_that_is_not_oar_gets_no_decision():
    """A VB (Min-Max managed) material has nothing to convert."""
    row = normal_row(oar_scope="OUT_OF_SCOPE")
    result = builder.build_normal_recommendation(row, UNSIGNED_POLICY)
    assert result.conversion is None


def test_obsolete_is_never_eligible_through_the_builder():
    row = oar_row(consumption_count_12m=25, criticality=Criticality.OBSOLETE.value)
    result = builder.build_oar_recommendation(row, UNSIGNED_POLICY)
    assert result.conversion.eligibility.value == "NOT_ELIGIBLE"


def test_the_builder_takes_no_hod_lookup():
    import inspect

    assert "hod_lookup" not in inspect.signature(builder.build_oar_recommendation).parameters


# --- FR-2 classification methodology itself must be untouched --------------


def test_evaluate_conversion_signature_still_has_no_demand_class_parameter():
    """Pins that demand_class cannot influence eligibility because there is
    no way to pass it in."""
    import inspect

    from app.initiatives.i7.recommendations.conversion import evaluate

    parameters = inspect.signature(evaluate).parameters
    assert "demand_class" not in parameters


def test_classify_demand_function_is_unmodified_in_signature():
    """A regression guard: this task must not touch FR-2 classification
    logic. If ADI/CV-squared thresholds or the classify_demand signature ever
    change, this test's import and call shape should be revisited deliberately
    -- not as a side effect of an OAR display fix."""
    import inspect

    from app.initiatives.i7.features.classification import classify_demand

    parameters = list(inspect.signature(classify_demand).parameters)
    assert parameters == ["adi", "cv_squared", "policy"]

"""OAR classification reaches the normal (forecast-backed) recommendation path.

An OAR material -- MRP type ND, PD, or blank/NULL -- with sufficient history is
forecast and sized through Phase 4/5 like any other: the OAR -> Min-Max
conversion suggestion needs a recommended ROP and Max, so stopping forecasting
for it would leave the suggestion with nothing to carry. Before this fix the
normal-path builder hardcoded ``is_oar=False`` and ``conversion=None``, so every
such material lost its OAR identity at the last step and was never evaluated
for conversion.

The OAR verdict is never re-derived here: ``oar_scope`` is read from the
feature store, where Phase 3 computed it with the one authoritative policy
(:func:`app.initiatives.i7.policy.oar.current_oar_policy`). The scope in each
test row is produced by that same policy, so these tests exercise the real
rule end to end rather than a hand-typed ``"IN_SCOPE"``.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.initiatives.i7.contracts import (
    MaterialAttributes,
    MaterialIdentity,
    MaterialPlantKey,
    PlantIdentity,
    ScopeDecision,
)
from app.initiatives.i7.features import assess_oar_scope
from app.initiatives.i7.policy import PolicyDocument, current_oar_policy
from app.initiatives.i7.recommendations import builder, repository, service
from app.initiatives.i7.recommendations.types import (
    ConversionEligibility,
    ConversionTrigger,
    LifecycleStatus,
)

POLICY = PolicyDocument()
OAR_POLICY = current_oar_policy()


def scope_for(mrp_type: str | None) -> str:
    """The feature-store ``oar_scope`` value Phase 3 writes for this MRP type."""
    attributes = MaterialAttributes(
        key=MaterialPlantKey(
            material=MaterialIdentity(sap_material_number="5000092261"),
            plant=PlantIdentity(sap_plant_code="1300"),
        ),
        mrp_type=mrp_type,
    )
    return assess_oar_scope(attributes, OAR_POLICY).scope.value


def normal_row(mrp_type: str | None, **overrides) -> SimpleNamespace:
    """A SUFFICIENT-history material with a complete Phase 5 result."""
    defaults = dict(
        sap_material_number="5000092261",
        sap_plant_code="1300",
        demand_class="INTERMITTENT",
        history_status="SUFFICIENT",
        criticality="NORMAL",
        non_zero_periods=6,
        consumption_count_12m=0,
        mrp_type=mrp_type,
        oar_scope=scope_for(mrp_type),
        feature_run_id=1,
        baseline_model="SBA",
        forecast_rate=Decimal("1.149129"),
        current_safety_stock=None,
        current_reorder_point=Decimal(0),
        current_maximum_stock=Decimal(0),
        unit_price=Decimal("2317.71"),
        lead_time_method="PLANNED_DELIVERY_TIME",
        valid_po_count=0,
        lt_avg_months=Decimal("0.46"),
        lt_avg_days=Decimal("14"),
        sigma_lt_days=None,
        circuit=None,
        service_level=Decimal("0.85"),
        z_factor=Decimal("1.036433"),
        safety_stock_status="SUCCESS",
        safety_stock_method="intermittent",
        safety_stock=2,
        detail=None,
        rop_status="SUCCESS",
        rop=3,
        max_stock_status="SUCCESS",
        max_stock_strategy="rop_plus_eoq",
        max_stock=5,
        inventory_run_id=96,
        forecast_run_id=11,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# --- The policy: ND, PD and blank are OAR; anything else is not --------------


@pytest.mark.parametrize("mrp_type", ["PD", "ND", None, "", "   "])
def test_policy_classifies_nd_pd_and_every_blank_form_as_oar(mrp_type):
    """SQL NULL arrives as Python ``None``; an empty or whitespace-only string
    is the same fact ("not maintained") and must not fall through to UNKNOWN
    or OUT_OF_SCOPE if it ever reaches the policy uncleaned."""
    assert scope_for(mrp_type) == ScopeDecision.IN_SCOPE.value


@pytest.mark.parametrize("mrp_type", ["VB", "V1", "M0", "RP"])
def test_policy_classifies_other_mrp_types_as_not_oar(mrp_type):
    assert scope_for(mrp_type) == ScopeDecision.OUT_OF_SCOPE.value


# --- 1-4: is_oar follows the feature store's OAR verdict ----------------------


@pytest.mark.parametrize("mrp_type", ["PD", "ND", None, ""], ids=["PD", "ND", "NULL", "blank"])
def test_oar_material_with_sufficient_history_is_marked_oar(mrp_type):
    result = builder.build_normal_recommendation(normal_row(mrp_type), POLICY)
    assert result.is_oar is True


def test_non_oar_material_with_sufficient_history_is_not_oar():
    result = builder.build_normal_recommendation(normal_row("VB"), POLICY)
    assert result.is_oar is False
    assert result.conversion is None


def test_unknown_scope_is_reported_as_undetermined_not_as_non_oar():
    """UNKNOWN cannot arise from a blank MRP type under the current policy, but
    a feature run built before the blank-as-OAR rule stored it for blanks. It
    must not be silently read as "not OAR"."""
    result = builder.build_normal_recommendation(
        normal_row(None, oar_scope=ScopeDecision.UNKNOWN.value), POLICY
    )
    assert result.is_oar is None
    assert result.conversion is None


# --- 5: an OAR material still carries its forecast, SS, ROP and Max -----------


@pytest.mark.parametrize("mrp_type", ["PD", "ND", None])
def test_oar_material_keeps_forecast_safety_stock_rop_and_max(mrp_type):
    result = builder.build_normal_recommendation(normal_row(mrp_type), POLICY)

    assert result.status is LifecycleStatus.READY_FOR_REVIEW
    assert result.forecast_rate == Decimal("1.149129")
    assert result.baseline_model == "SBA"
    assert result.recommended_safety_stock == 2
    assert result.recommended_rop == 3
    assert result.recommended_max_stock == 5
    assert result.inventory_run_id == 96
    assert result.forecast_run_id == 11
    assert result.oar_run_id is None


def test_oar_explanation_keeps_the_forecast_factors_and_adds_the_conversion():
    """This material is OAR by MRP type, not cold-start: its explanation must
    still describe the forecast that produced its numbers."""
    result = builder.build_normal_recommendation(normal_row("PD"), POLICY)
    labels = [factor.label for factor in result.factors]

    assert "OAR cold-start" not in labels
    assert "Forecasting model" in labels
    assert "OAR -> Min-Max conversion" in labels


# --- 6: the existing conversion trigger logic is evaluated --------------------


def test_oar_material_is_evaluated_by_the_existing_conversion_logic(monkeypatch):
    calls = []
    real = builder.evaluate_conversion

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(builder, "evaluate_conversion", spy)
    lookup = SimpleNamespace(hod_approved=lambda material, plant: None)
    builder.build_normal_recommendation(
        normal_row("PD", consumption_count_12m=7), POLICY, hod_lookup=lookup
    )

    assert calls == [
        dict(
            material="5000092261",
            plant="1300",
            consumption_count_12m=7,
            criticality="NORMAL",
            policy=POLICY.conversion_triggers,
            hod_lookup=lookup,
        )
    ]


def test_non_oar_material_never_calls_the_conversion_logic(monkeypatch):
    def fail(**kwargs):
        raise AssertionError("conversion evaluated for a non-OAR material")

    monkeypatch.setattr(builder, "evaluate_conversion", fail)
    builder.build_normal_recommendation(normal_row("VB"), POLICY)


def test_conversion_fires_on_consumption_frequency():
    result = builder.build_normal_recommendation(
        normal_row("ND", consumption_count_12m=7, criticality="CRITICAL"), POLICY
    )
    assert result.conversion.eligibility is ConversionEligibility.ELIGIBLE
    assert result.conversion.trigger is ConversionTrigger.CONSUMPTION_FREQUENCY


def test_conversion_fires_on_the_confirmed_criticality_tier():
    result = builder.build_normal_recommendation(normal_row(None), POLICY)
    assert result.conversion.eligibility is ConversionEligibility.ELIGIBLE
    assert result.conversion.trigger is ConversionTrigger.PRODUCTION_IMPACT


def test_conversion_fields_are_persisted_for_an_oar_normal_path_row():
    row = service._to_row(
        builder.build_normal_recommendation(normal_row("PD", consumption_count_12m=7), POLICY)
    )

    assert row["is_oar"] is True
    assert row["recommendation_id"] == "REC-OAR-5000092261-1300"
    assert row["conversion_eligibility"] == ConversionEligibility.ELIGIBLE.value
    assert row["conversion_trigger"] == ConversionTrigger.CONSUMPTION_FREQUENCY.value
    assert row["consumption_count_12m"] == 7
    assert row["recommended_rop"] == 3
    assert row["recommended_max_stock"] == 5


# --- 7: non-OAR behaviour is unchanged ----------------------------------------


def test_non_oar_recommendation_is_unchanged():
    result = builder.build_normal_recommendation(normal_row("VB"), POLICY)
    row = service._to_row(result)

    assert result.status is LifecycleStatus.READY_FOR_REVIEW
    assert result.recommended_safety_stock == 2
    assert result.recommended_rop == 3
    assert result.recommended_max_stock == 5
    assert [factor.label for factor in result.factors] == [
        "Demand pattern",
        "Forecasting model",
        "Lead time",
        "Reorder point",
    ]
    assert row["recommendation_id"] == "REC-STD-5000092261-1300"
    assert row["conversion_eligibility"] is None
    assert row["conversion_trigger"] is None


# --- The query supplies what the builder now reads ----------------------------


def test_normal_input_query_selects_the_oar_verdict_and_conversion_inputs():
    for column in ("f.oar_scope", "f.mrp_type", "f.consumption_count_12m"):
        assert column in repository._NORMAL_SQL

"""Detail-API field completeness: circuit, unit price, numeric lead time and
its variance, service level and Z-factor.

These are all Phase 5 (or Phase 3) values the recommendation builder already
read -- previously only embedded as text inside calculation_trace, now typed
fields on BuiltRecommendation/Recommendation/RecommendationDetail. Nothing
here is recomputed: every assertion pins that a value already on the input
row survives unchanged to the schema.
"""

from decimal import Decimal
from types import SimpleNamespace

from app.initiatives.i7.recommendations import builder
from app.initiatives.i7.policy import PolicyDocument


def normal_row(**overrides) -> SimpleNamespace:
    defaults = dict(
        sap_material_number="1000000001",
        sap_plant_code="1300",
        demand_class="SMOOTH",
        history_status="SUFFICIENT",
        criticality="CRITICAL",
        non_zero_periods=10,
        feature_run_id=1,
        baseline_model="SES",
        forecast_rate=Decimal("5.83"),
        current_safety_stock=Decimal(2),
        current_reorder_point=Decimal(4),
        current_maximum_stock=Decimal(6),
        unit_price=Decimal("125.50"),
        lead_time_method="ACTUAL_STATISTICAL",
        valid_po_count=6,
        lt_avg_months=Decimal("3.99"),
        lt_avg_days=Decimal("121.4"),
        sigma_lt_days=Decimal("12.0"),
        circuit="CRUSHING",
        service_level=Decimal("0.95"),
        z_factor=Decimal("1.645"),
        safety_stock_status="SUCCESS",
        safety_stock_method="normal_path_z_sigma",
        safety_stock=Decimal(8),
        detail=None,
        rop_status="SUCCESS",
        rop=Decimal(15),
        max_stock_status="NOT_CONFIGURED",
        max_stock_strategy=None,
        max_stock=None,
        inventory_run_id=1,
        forecast_run_id=1,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


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
        unit_price=Decimal("42.10"),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


POLICY = PolicyDocument()


# --- Normal path: values carried through, never recomputed ------------------


def test_normal_recommendation_exposes_circuit():
    result = builder.build_normal_recommendation(normal_row(circuit="MILLING"), POLICY)
    assert result.circuit == "MILLING"


def test_normal_recommendation_exposes_unit_price():
    result = builder.build_normal_recommendation(
        normal_row(unit_price=Decimal("99.99")), POLICY
    )
    assert result.unit_price == Decimal("99.99")


def test_normal_recommendation_exposes_lead_time_days_and_variance():
    result = builder.build_normal_recommendation(
        normal_row(lt_avg_days=Decimal("60.5"), sigma_lt_days=Decimal("5.5")), POLICY
    )
    assert result.lead_time_days == Decimal("60.5")
    assert result.lead_time_variance_days == Decimal("5.5")


def test_normal_recommendation_exposes_service_level_and_z_factor():
    result = builder.build_normal_recommendation(
        normal_row(service_level=Decimal("0.98"), z_factor=Decimal("2.05")), POLICY
    )
    assert result.service_level == Decimal("0.98")
    assert result.z_factor == Decimal("2.05")


def test_normal_recommendation_service_level_null_stays_null_not_defaulted():
    """The unsigned-matrix case: service_level/z_factor genuinely absent, not
    a fabricated default."""
    result = builder.build_normal_recommendation(
        normal_row(service_level=None, z_factor=None), POLICY
    )
    assert result.service_level is None
    assert result.z_factor is None


# --- OAR path: circuit/lead-time/service-level are genuinely unavailable ---
# --- (never reached Phase 5); unit_price is a Phase 3 field, still present -


def test_oar_recommendation_has_no_circuit():
    result = builder.build_oar_recommendation(oar_row(), POLICY)
    assert result.circuit is None


def test_oar_recommendation_has_no_lead_time_or_service_level():
    result = builder.build_oar_recommendation(oar_row(), POLICY)
    assert result.lead_time_days is None
    assert result.lead_time_variance_days is None
    assert result.service_level is None
    assert result.z_factor is None


def _signed_policy() -> PolicyDocument:
    from app.initiatives.i7.contracts import Criticality
    from app.initiatives.i7.policy import ServiceLevelKey, ServiceLevelPolicy

    return PolicyDocument(
        service_level=ServiceLevelPolicy(
            matrix=(
                (ServiceLevelKey(criticality=Criticality.NORMAL), 0.85),
                (ServiceLevelKey(criticality=Criticality.CRITICAL), 0.98),
            )
        )
    )


def test_oar_recommendation_takes_the_tier_service_level_and_z_from_the_signed_matrix():
    result = builder.build_oar_recommendation(
        oar_row(criticality="NORMAL"), _signed_policy()
    )
    assert result.service_level == Decimal("0.85")
    assert abs(float(result.z_factor) - 1.036) < 0.001
    # Lead time stays unavailable: that one really is a Phase 5 output.
    assert result.lead_time_days is None


def test_oar_recommendation_tier_is_read_case_and_space_insensitively():
    result = builder.build_oar_recommendation(
        oar_row(criticality=" critical "), _signed_policy()
    )
    assert result.service_level == Decimal("0.98")


def test_oar_recommendation_without_a_tier_gets_no_service_level_not_a_default():
    result = builder.build_oar_recommendation(oar_row(criticality=None), _signed_policy())
    assert result.service_level is None and result.z_factor is None


def test_oar_recommendation_with_an_unrecognised_tier_gets_no_service_level():
    result = builder.build_oar_recommendation(
        oar_row(criticality="PLATINUM"), _signed_policy()
    )
    assert result.service_level is None and result.z_factor is None


def test_oar_recommendation_with_an_unsigned_matrix_gets_no_service_level():
    result = builder.build_oar_recommendation(oar_row(criticality="NORMAL"), POLICY)
    assert result.service_level is None and result.z_factor is None


def test_oar_recommendation_still_exposes_unit_price():
    result = builder.build_oar_recommendation(oar_row(unit_price=Decimal("7.25")), POLICY)
    assert result.unit_price == Decimal("7.25")


# --- Schema level: the API response actually carries these fields ----------


def test_recommendation_detail_schema_has_the_new_fields():
    from app.schemas.i7.recommendations import (
        LeadTimeInfo,
        RecommendationDetail,
        ServiceLevelInfo,
    )

    assert "circuit" in RecommendationDetail.model_fields
    assert "unit_price" in RecommendationDetail.model_fields
    assert "service_level" in RecommendationDetail.model_fields
    assert "days" in LeadTimeInfo.model_fields
    assert "variance_days" in LeadTimeInfo.model_fields
    assert "service_level" in ServiceLevelInfo.model_fields
    assert "z_factor" in ServiceLevelInfo.model_fields


def test_service_level_info_defaults_to_none_not_fabricated():
    from app.schemas.i7.recommendations import ServiceLevelInfo

    info = ServiceLevelInfo()
    assert info.service_level is None
    assert info.z_factor is None


def test_a_recommendation_whose_forecast_came_from_tsb_says_demand_is_declining():
    result = builder.build_normal_recommendation(normal_row(forecast_model="TSB"), POLICY)
    assert [f.label for f in result.factors].count("Declining demand") == 1
    assert "How demand was projected" not in [f.label for f in result.factors]


def test_a_row_without_a_forecast_model_column_still_builds():
    # Older callers and fixtures have no forecast_model attribute at all.
    result = builder.build_normal_recommendation(normal_row(), POLICY)
    assert "Declining demand" not in [f.label for f in result.factors]

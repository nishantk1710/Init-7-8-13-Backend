"""Canonical contract tests.

Weighted toward the invariants that fail *silently* if broken. A contract that
rejects a blank material number is mildly useful; one that rejects a gap in a
consumption series prevents a misclassification nobody would ever notice.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.initiatives.i7.contracts import (
    ConsumptionObservation,
    ConsumptionSeries,
    Criticality,
    DemandPattern,
    LeadTimeSource,
    MaterialAttributes,
    MaterialIdentity,
    MaterialPlantKey,
    PlantIdentity,
    PurchaseOrderObservation,
    Recommendation,
    RecommendationStatus,
    RiskLevel,
    StockParameters,
)
from app.initiatives.i7.contracts.recommendation import LeadTimeProfile
from app.initiatives.i7.policy import PolicyVersionRef


def make_key(material: str = "000000000010000000", plant: str = "1300") -> MaterialPlantKey:
    return MaterialPlantKey(
        material=MaterialIdentity(sap_material_number=material),
        plant=PlantIdentity(sap_plant_code=plant),
    )


# --- Identity ---------------------------------------------------------


def test_material_identity_keeps_both_vocabularies_separate():
    identity = MaterialIdentity(
        sap_material_number="000000000010000000",
        app_material_id="500-14892",
        description="Gearbox Bearing 55KW SKF",
    )
    assert identity.sap_material_number == "000000000010000000"
    assert identity.app_material_id == "500-14892"


def test_app_identity_is_optional_and_never_derived():
    """The mapping table does not exist; absence must stay absence."""
    identity = MaterialIdentity(sap_material_number="000000000010000000")
    assert identity.app_material_id is None


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_sap_material_number_rejected(blank):
    with pytest.raises(PydanticValidationError):
        MaterialIdentity(sap_material_number=blank)


def test_material_plant_key_is_the_grain_of_evaluation():
    key = make_key()
    assert str(key) == "000000000010000000|1300"


# --- Material attributes ----------------------------------------------


def test_material_attributes_require_only_identity():
    """Most business fields are absent for most of the real catalogue."""
    attributes = MaterialAttributes(key=make_key())
    assert attributes.mrp_type is None
    assert attributes.criticality is None
    assert attributes.circuit is None
    assert attributes.unit_price is None


def test_material_attributes_carry_both_oar_fields_and_retired_extwg():
    attributes = MaterialAttributes(
        key=make_key(),
        mrp_type="PD",
        material_status="01",
        external_material_group="100",
    )
    assert attributes.mrp_type == "PD"
    assert attributes.material_status == "01"
    # Present for source fidelity; the OAR rule may not read it.
    assert attributes.external_material_group == "100"


def test_unit_price_is_decimal_not_float():
    attributes = MaterialAttributes(key=make_key(), unit_price=Decimal("48200.55"))
    assert attributes.unit_price == Decimal("48200.55")


def test_negative_planned_delivery_time_rejected():
    with pytest.raises(PydanticValidationError):
        MaterialAttributes(key=make_key(), planned_delivery_time_days=-1)


# --- Consumption ------------------------------------------------------


def test_zero_demand_is_representable_and_flagged():
    observation = ConsumptionObservation(period=date(2026, 3, 1), quantity=Decimal("0"))
    assert observation.is_zero_demand is True


def test_period_is_normalised_to_first_of_month():
    observation = ConsumptionObservation(period=date(2026, 3, 17), quantity=Decimal("4"))
    assert observation.period == date(2026, 3, 1)


def test_negative_quantity_rejected():
    with pytest.raises(PydanticValidationError):
        ConsumptionObservation(period=date(2026, 3, 1), quantity=Decimal("-1"))


def test_series_counts_total_and_non_zero_periods():
    """n and n_nz -- the two inputs to ADI."""
    series = ConsumptionSeries(
        key=make_key(),
        observations=tuple(
            ConsumptionObservation(period=date(2026, month, 1), quantity=Decimal(qty))
            for month, qty in [(1, 4), (2, 0), (3, 6), (4, 0), (5, 0), (6, 5)]
        ),
    )
    assert series.total_periods == 6
    assert series.non_zero_periods == 3


def test_gap_in_series_rejected():
    """A missing month is indistinguishable from a zero one once it reaches ADI."""
    with pytest.raises(PydanticValidationError, match="gap in consumption series"):
        ConsumptionSeries(
            key=make_key(),
            observations=(
                ConsumptionObservation(period=date(2026, 1, 1), quantity=Decimal("4")),
                ConsumptionObservation(period=date(2026, 3, 1), quantity=Decimal("6")),
            ),
        )


def test_out_of_order_series_rejected():
    with pytest.raises(PydanticValidationError, match="ascending period order"):
        ConsumptionSeries(
            key=make_key(),
            observations=(
                ConsumptionObservation(period=date(2026, 2, 1), quantity=Decimal("4")),
                ConsumptionObservation(period=date(2026, 1, 1), quantity=Decimal("6")),
            ),
        )


def test_series_spanning_year_boundary_is_contiguous():
    series = ConsumptionSeries(
        key=make_key(),
        observations=(
            ConsumptionObservation(period=date(2025, 12, 1), quantity=Decimal("4")),
            ConsumptionObservation(period=date(2026, 1, 1), quantity=Decimal("0")),
        ),
    )
    assert series.total_periods == 2


# --- Lead time --------------------------------------------------------


def test_lead_time_days_derived_from_dates():
    observation = PurchaseOrderObservation(
        key=make_key(),
        purchasing_document="4500001234",
        created_on=date(2026, 1, 1),
        goods_receipt_date=date(2026, 4, 11),
    )
    assert observation.lead_time_days == 100


def test_open_purchase_order_has_no_lead_time():
    observation = PurchaseOrderObservation(
        key=make_key(), purchasing_document="4500001234", created_on=date(2026, 1, 1)
    )
    assert observation.lead_time_days is None


def test_receipt_before_creation_rejected():
    with pytest.raises(PydanticValidationError, match="precedes created_on"):
        PurchaseOrderObservation(
            key=make_key(),
            purchasing_document="4500001234",
            created_on=date(2026, 4, 11),
            goods_receipt_date=date(2026, 1, 1),
        )


def test_implausible_lead_time_is_not_rejected_by_the_contract():
    """Plausibility is configured policy, not a constant baked into a contract."""
    observation = PurchaseOrderObservation(
        key=make_key(),
        purchasing_document="4500001234",
        created_on=date(2020, 1, 1),
        goods_receipt_date=date(2026, 1, 1),
    )
    assert observation.lead_time_days == 2192


# --- Recommendation ---------------------------------------------------


def _minimal_recommendation(**overrides) -> Recommendation:
    defaults = dict(
        recommendation_id="REC-000001",
        key=make_key(),
        policy=PolicyVersionRef(policy_id="i07-default", policy_version=1),
        generated_at=datetime(2026, 8, 18, 9, 5),
    )
    defaults.update(overrides)
    return Recommendation(**defaults)


def test_recommendation_requires_a_policy_reference():
    """Without it a recommendation is not auditable, so there is no default."""
    with pytest.raises(PydanticValidationError):
        Recommendation(
            recommendation_id="REC-000001",
            key=make_key(),
            generated_at=datetime(2026, 8, 18, 9, 5),
        )


def test_unavailable_values_are_none_not_zero():
    """The failure this guards: 0 means "unknown" and Phi^-1(0) is -inf."""
    recommendation = _minimal_recommendation()
    assert recommendation.service_level_target is None
    assert recommendation.unit_price is None
    assert recommendation.lead_time is None
    assert recommendation.recommended.safety_stock is None
    assert recommendation.working_capital_impact is None


def test_service_level_zero_is_rejected():
    with pytest.raises(PydanticValidationError):
        _minimal_recommendation(service_level_target=0.0)


def test_service_level_one_is_rejected():
    """A 100% service level implies infinite safety stock."""
    with pytest.raises(PydanticValidationError):
        _minimal_recommendation(service_level_target=1.0)


def test_criticality_and_risk_are_independent_axes():
    """A critical material at low risk must be expressible."""
    recommendation = _minimal_recommendation(
        criticality=Criticality.CRITICAL, risk=RiskLevel.LOW
    )
    assert recommendation.criticality is Criticality.CRITICAL
    assert recommendation.risk is RiskLevel.LOW
    assert recommendation.criticality.value != recommendation.risk.value


def test_criticality_uses_sap_taxonomy_not_the_ui_scale():
    assert {tier.value for tier in Criticality} == {
        "CRITICAL",
        "IMPACT",
        "INSURANCE",
        "NORMAL",
        "OBSOLETE",
    }


def test_unclassified_is_the_default_demand_pattern():
    """Materials that fail the history gate never get a pattern."""
    assert _minimal_recommendation().demand_pattern is DemandPattern.UNCLASSIFIED


def test_oar_unknown_is_representable():
    assert _minimal_recommendation().is_oar is None


def test_lead_time_profile_records_provenance():
    recommendation = _minimal_recommendation(
        lead_time=LeadTimeProfile(
            source=LeadTimeSource.PLANNED_DELIVERY_TIME, mean_days=45.0
        )
    )
    assert recommendation.lead_time.source is LeadTimeSource.PLANNED_DELIVERY_TIME
    # No PO history to measure spread from -- absent, not zero.
    assert recommendation.lead_time.standard_deviation_days is None


def test_partially_known_stock_parameters_are_valid():
    parameters = StockParameters(reorder_point=Decimal("32"))
    assert parameters.reorder_point == Decimal("32")
    assert parameters.maximum_stock is None


def test_default_status_is_pending_review():
    assert _minimal_recommendation().status is RecommendationStatus.PENDING_REVIEW


def test_recommendation_is_immutable():
    recommendation = _minimal_recommendation()
    with pytest.raises(PydanticValidationError):
        recommendation.recommendation_id = "REC-000002"

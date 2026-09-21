"""Part 30 -- the recommendation's rationale describes the persisted
calculation it was actually built from, not whatever policy happens to be
active when a later ``generate_recommendations()`` call runs.

Part 29 audit found the defect: ``build_normal_recommendation`` passed
``service_level_configured=policy.service_level.is_configured`` -- the
CURRENT call's policy -- into ``explanation.build_factors()``. A
recommendation calculated under the DEV-mock service-level policy (Part 25)
could therefore claim "service-level policy is not configured" the moment a
later call ran with the mock off, even though its own persisted
``i7_inventory_calculation.service_level``/``z_factor`` were real, non-NULL
values from a successful calculation.

The fix reads ``row.service_level is not None`` instead --
``inventory/service_level.resolve()`` only ever sets ``service_level``/
``z_factor`` on its ``SUCCESS`` path (every ``NOT_EVALUABLE_*``/
``CALCULATION_ERROR`` branch leaves both ``None``), so this is an exact,
already-persisted, already-selected proxy for "this specific calculation
had a configured service level" -- immune to whatever policy is active
later.
"""

from decimal import Decimal
from types import SimpleNamespace

from app.initiatives.i7.recommendations import builder
from app.initiatives.i7.recommendations.types import LifecycleStatus
from app.initiatives.i7.policy import PolicyDocument, ServiceLevelKey, ServiceLevelPolicy
from app.initiatives.i7.contracts.enums import Criticality

UNSIGNED_POLICY = PolicyDocument()

SIGNED_POLICY = PolicyDocument(
    service_level=ServiceLevelPolicy(
        matrix=tuple((ServiceLevelKey(criticality=tier), 0.95) for tier in Criticality)
    )
)


def normal_row(**overrides) -> SimpleNamespace:
    defaults = dict(
        sap_material_number="5000092261",
        sap_plant_code="1300",
        demand_class="INTERMITTENT",
        history_status="SUFFICIENT",
        criticality="NORMAL",
        non_zero_periods=6,
        feature_run_id=67,
        baseline_model="SBA",
        forecast_rate=Decimal("1.149129"),
        current_safety_stock=None,
        current_reorder_point=Decimal(0),
        current_maximum_stock=Decimal(0),
        unit_price=Decimal("2317.71"),
        lead_time_method="PLANNED_FALLBACK",
        valid_po_count=1,
        lt_avg_months=Decimal("0.46"),
        lt_avg_days=Decimal("14.0"),
        sigma_lt_days=Decimal("4.2"),
        circuit=None,
        service_level=Decimal("0.85"),
        z_factor=Decimal("1.036433"),
        safety_stock_status="SUCCESS",
        safety_stock_method="compound_poisson",
        safety_stock=2,
        detail=None,
        rop_status="SUCCESS",
        rop=3,
        max_stock_status="NOT_CONFIGURED",
        max_stock_strategy=None,
        max_stock=None,
        inventory_run_id=85,
        forecast_run_id=10,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _blocked_factor(result):
    return next((f for f in result.factors if f.label == "Blocked"), None)


# --- 1: DEV mock produced the row, later call has the mock off -------------


def test_recommendation_built_under_dev_mock_is_not_mislabelled_once_the_mock_is_off():
    """The exact Part 29/30 scenario: the row (as persisted by an inventory
    run computed under the DEV service-level mock) has real service_level/
    z_factor. Building the recommendation with UNSIGNED_POLICY -- simulating
    a later generate_recommendations() call with the DEV flag off -- must
    NOT produce the misleading 'service-level policy is not configured'
    factor, because the policy passed in is irrelevant to what this row's
    own calculation actually had.
    """
    row = normal_row()  # service_level=0.85, z_factor=1.036433, SUCCESS
    result = builder.build_normal_recommendation(row, UNSIGNED_POLICY)

    blocked = _blocked_factor(result)
    assert blocked is None, (
        f"recommendation wrongly claims unconfigured service level: {blocked}"
    )
    assert result.status is LifecycleStatus.READY_FOR_REVIEW
    assert result.recommended_safety_stock == 2
    assert result.recommended_rop == 3


# --- 2: valid persisted service level -> rationale consistent with it ------


def test_rationale_is_consistent_with_a_valid_persisted_service_level():
    """A row whose own calculation succeeded must never carry the 'blocked'
    factor, regardless of which PolicyDocument the caller passes -- signed
    or unsigned. The row's own recorded outcome is authoritative."""
    row = normal_row(service_level=Decimal("0.98"), z_factor=Decimal("2.05"))

    for policy in (UNSIGNED_POLICY, SIGNED_POLICY):
        result = builder.build_normal_recommendation(row, policy)
        assert _blocked_factor(result) is None


# --- 3: genuinely unconfigured calculation still blocks correctly ----------


def test_genuinely_unconfigured_calculation_still_produces_the_blocked_factor():
    """A row whose OWN calculation never resolved a service level (service_
    level/z_factor both None, matching inventory/service_level.resolve()'s
    NOT_EVALUABLE_SERVICE_LEVEL_UNSET path) must still show the blocked
    explanation -- even if the CURRENT caller happens to pass a signed
    policy. The row's own persisted outcome, not the caller's policy, is
    what must gate this now.
    """
    row = normal_row(
        service_level=None,
        z_factor=None,
        safety_stock_status="NOT_EVALUABLE_SERVICE_LEVEL_UNSET",
        safety_stock=None,
        rop_status="NOT_EVALUABLE_SERVICE_LEVEL_UNSET",
        rop=None,
        detail="the Criticality x Circuit service-level matrix must be supplied and signed",
    )

    for policy in (UNSIGNED_POLICY, SIGNED_POLICY):
        result = builder.build_normal_recommendation(row, policy)
        blocked = _blocked_factor(result)
        assert blocked is not None
        assert "service-level policy is not configured" in blocked.detail
        assert result.status is LifecycleStatus.NOT_EVALUABLE


# --- 4: current PolicyDocument is irrelevant to an already-persisted row ---


def test_changing_the_current_policy_does_not_change_an_already_persisted_rows_explanation():
    """The single row is built twice under two different PolicyDocuments.
    The explanation (blocked or not) must be identical both times, because
    it is now derived entirely from the row's own persisted service_level,
    never from the policy argument.
    """
    configured_row = normal_row()
    unconfigured_row = normal_row(
        service_level=None, z_factor=None,
        safety_stock_status="NOT_EVALUABLE_SERVICE_LEVEL_UNSET", safety_stock=None,
        rop_status="NOT_EVALUABLE_SERVICE_LEVEL_UNSET", rop=None,
    )

    for row in (configured_row, unconfigured_row):
        blocked_under_unsigned = _blocked_factor(builder.build_normal_recommendation(row, UNSIGNED_POLICY))
        blocked_under_signed = _blocked_factor(builder.build_normal_recommendation(row, SIGNED_POLICY))
        assert (blocked_under_unsigned is None) == (blocked_under_signed is None)


# --- 5: unrelated gates (lead time) are unaffected --------------------------


def test_lead_time_blocked_factor_is_unaffected_by_this_fix():
    """This fix only changes the service_level_configured input to
    build_factors -- the lead-time NOT_EVALUABLE path (a different gate
    entirely) must behave exactly as before."""
    row = normal_row(
        lead_time_method=None,
        safety_stock_status="NOT_EVALUABLE_LEAD_TIME",
        safety_stock=None,
        rop_status="NOT_EVALUABLE_LEAD_TIME",
        rop=None,
        detail="no usable lead time",
    )
    result = builder.build_normal_recommendation(row, SIGNED_POLICY)
    assert result.status is LifecycleStatus.NOT_EVALUABLE
    assert result.recommended_safety_stock is None


# --- 6: SS/ROP values are untouched by this fix -----------------------------


def test_ss_and_rop_values_are_unchanged_by_the_rationale_fix():
    row = normal_row()
    result = builder.build_normal_recommendation(row, UNSIGNED_POLICY)
    assert result.recommended_safety_stock == 2
    assert result.recommended_rop == 3

    result_signed = builder.build_normal_recommendation(row, SIGNED_POLICY)
    assert result_signed.recommended_safety_stock == 2
    assert result_signed.recommended_rop == 3

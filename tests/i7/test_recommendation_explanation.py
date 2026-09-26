"""Deterministic reasons, calculation trace, and expected impact.

No LLM. Every factor must trace to a fact actually passed in.
"""

from decimal import Decimal

from app.initiatives.i7.recommendations.explanation import (
    build_factors,
    build_trace,
    blocked_trace,
    expected_impact,
    parameter_delta,
)
from app.initiatives.i7.recommendations.types import ImpactStatus, LifecycleStatus


def test_parameter_delta_computes_percent_change():
    delta = parameter_delta(Decimal(10), Decimal(15))
    assert delta.delta == Decimal(5)
    assert delta.percent_change == Decimal(50)


def test_parameter_delta_is_none_when_current_is_zero():
    """A percentage of nothing has no meaning; 0% would misleadingly read as
    "no change"."""
    delta = parameter_delta(Decimal(0), Decimal(5))
    assert delta.percent_change is None


def test_parameter_delta_is_none_when_either_value_is_missing():
    assert parameter_delta(None, Decimal(5)).delta is None
    assert parameter_delta(Decimal(5), None).delta is None


def test_expected_impact_computes_all_three_deltas():
    impact = expected_impact(
        current_ss=Decimal(2), recommended_ss=Decimal(19),
        current_rop=Decimal(4), recommended_rop=Decimal(32),
        current_max=Decimal(6), recommended_max=Decimal(29),
        unit_price=None, holding_cost_rate=None,
    )
    assert impact.status is ImpactStatus.AVAILABLE
    assert impact.safety_stock.delta == Decimal(17)
    assert impact.reorder_point.delta == Decimal(28)
    assert impact.maximum_stock.delta == Decimal(23)


def test_monetary_impact_is_unavailable_without_cost_data():
    """No holding rate exists in any I07 document or table."""
    impact = expected_impact(
        current_ss=Decimal(2), recommended_ss=Decimal(19),
        current_rop=None, recommended_rop=None,
        current_max=None, recommended_max=None,
        unit_price=Decimal(12500), holding_cost_rate=None,
    )
    assert impact.monetary_impact is None
    assert "holding rate" in (impact.detail or "")


def test_missing_recommended_values_is_not_evaluable():
    impact = expected_impact(
        current_ss=Decimal(2), recommended_ss=None,
        current_rop=Decimal(4), recommended_rop=None,
        current_max=Decimal(6), recommended_max=None,
        unit_price=None, holding_cost_rate=None,
    )
    assert impact.status is ImpactStatus.NOT_EVALUABLE_MISSING_RECOMMENDED


def test_missing_current_values_is_not_evaluable():
    impact = expected_impact(
        current_ss=None, recommended_ss=Decimal(19),
        current_rop=None, recommended_rop=Decimal(32),
        current_max=None, recommended_max=Decimal(29),
        unit_price=None, holding_cost_rate=None,
    )
    assert impact.status is ImpactStatus.NOT_EVALUABLE_MISSING_CURRENT


def test_factors_for_a_normal_recommendation_are_traceable():
    factors = build_factors(
        demand_class="INTERMITTENT",
        baseline_model="SBA",
        lead_time_detail="ACTUAL_STATISTICAL, 6 PO(s)",
        service_level_configured=True,
        recommended_rop=Decimal(32),
        current_rop=Decimal(4),
        is_oar=False,
        history_status="SUFFICIENT",
        oar_neighbour_count=None,
        oar_confidence=None,
    )
    labels = [factor.label for factor in factors]
    assert "Demand pattern" in labels
    assert "Forecasting model" in labels
    assert any("INTERMITTENT" in factor.detail for factor in factors)
    assert any("SBA" in factor.detail for factor in factors)


def test_no_unsupported_savings_claim_without_data():
    """Reasons must never claim inventory reduction, savings, or better
    service unless a computed value actually supports the statement."""
    factors = build_factors(
        demand_class="SMOOTH",
        baseline_model="SES",
        lead_time_detail=None,
        service_level_configured=False,
        recommended_rop=None,
        current_rop=None,
        is_oar=False,
        history_status="SUFFICIENT",
        oar_neighbour_count=None,
        oar_confidence=None,
    )
    text = " ".join(factor.detail for factor in factors).lower()
    for forbidden in ("savings", "reduction", "better service", "improved"):
        assert forbidden not in text


def test_blocked_recommendation_states_the_reason():
    factors = build_factors(
        demand_class="INTERMITTENT",
        baseline_model="SBA",
        lead_time_detail=None,
        service_level_configured=False,
        recommended_rop=None,
        current_rop=None,
        is_oar=False,
        history_status="SUFFICIENT",
        oar_neighbour_count=None,
        oar_confidence=None,
    )
    assert any("blocked" in factor.detail.lower() for factor in factors)


def test_oar_factors_mention_neighbour_count_and_confidence():
    factors = build_factors(
        demand_class=None,
        baseline_model=None,
        lead_time_detail=None,
        service_level_configured=False,
        recommended_rop=None,
        current_rop=None,
        is_oar=True,
        history_status="COLD_START",
        oar_neighbour_count=4,
        oar_confidence="MEDIUM",
    )
    text = " ".join(factor.detail for factor in factors)
    assert "4" in text
    assert "MEDIUM" in text


def test_blocked_trace_names_the_status_and_reason():
    trace = blocked_trace(LifecycleStatus.NOT_EVALUABLE, "service level unsigned")
    entries = dict(trace.entries)
    assert entries["blocked_status"] == "NOT_EVALUABLE"
    assert entries["reason"] == "service level unsigned"


def test_build_trace_preserves_order():
    trace = build_trace([("a", "1"), ("b", "2")])
    assert trace.entries == (("a", "1"), ("b", "2"))

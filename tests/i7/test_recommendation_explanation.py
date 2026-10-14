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
    assert "Usage pattern" in labels
    assert "How demand was projected" in labels
    assert any("occasional" in factor.detail for factor in factors)
    assert any("now and then" in factor.detail for factor in factors)


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
    assert any(factor.label == "Blocked" for factor in factors)
    assert any("no recommendation can be made" in factor.detail.lower() for factor in factors)


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
    assert "4 similar materials" in text
    assert "medium" in text.lower()


def test_blocked_trace_names_the_status_and_reason():
    trace = blocked_trace(LifecycleStatus.NOT_EVALUABLE, "service level unsigned")
    entries = dict(trace.entries)
    assert entries["blocked_status"] == "NOT_EVALUABLE"
    assert entries["reason"] == "service level unsigned"


def test_build_trace_preserves_order():
    trace = build_trace([("a", "1"), ("b", "2")])
    assert trace.entries == (("a", "1"), ("b", "2"))


# --- Wording a non-technical reader can follow ---------------------------------


def test_an_unclassified_material_says_nothing_about_a_usage_pattern():
    factors = build_factors(
        demand_class="UNCLASSIFIED",
        baseline_model=None,
        lead_time_detail=None,
        service_level_configured=True,
        recommended_rop=None,
        current_rop=None,
        is_oar=True,
        history_status="NO_HISTORY",
        oar_neighbour_count=10,
        oar_confidence="MEDIUM",
    )
    text = " ".join(factor.detail for factor in factors)
    assert "UNCLASSIFIED" not in text
    assert "no recorded usage" in text
    assert "10 similar materials" in text


def test_no_factor_leaks_internal_codes_or_requirement_numbers():
    for history_status in ("NO_HISTORY", "COLD_START"):
        factors = build_factors(
            demand_class="UNCLASSIFIED",
            baseline_model=None,
            lead_time_detail=None,
            service_level_configured=True,
            recommended_rop=None,
            current_rop=None,
            is_oar=True,
            history_status=history_status,
            oar_neighbour_count=3,
            oar_confidence="LOW",
        )
        text = " ".join(factor.detail for factor in factors)
        for jargon in ("FR-2", "NO_HISTORY", "COLD_START", "OAR cold-start", "neighbour(s)"):
            assert jargon not in text


def test_reorder_point_change_reads_as_a_sentence_with_tidy_numbers():
    rising = build_factors(
        demand_class=None, baseline_model=None, lead_time_detail=None,
        service_level_configured=True, recommended_rop=Decimal("1500.000"),
        current_rop=Decimal("800"), is_oar=False, history_status="SUFFICIENT",
        oar_neighbour_count=None, oar_confidence=None,
    )
    assert "rise from 800 to 1,500" in rising[-1].detail
    falling = build_factors(
        demand_class=None, baseline_model=None, lead_time_detail=None,
        service_level_configured=True, recommended_rop=Decimal("3.0"),
        current_rop=Decimal("16"), is_oar=False, history_status="SUFFICIENT",
        oar_neighbour_count=None, oar_confidence=None,
    )
    assert "fall from 16 to 3" in falling[-1].detail


# --- the declining-demand path says so ---------------------------------------------


def _normal_factors(**overrides):
    arguments = dict(
        demand_class="INTERMITTENT", baseline_model="SBA", lead_time_detail=None,
        service_level_configured=True, recommended_rop=Decimal(32), current_rop=Decimal(40),
        is_oar=False, history_status="SUFFICIENT", oar_neighbour_count=None, oar_confidence=None,
    )
    arguments.update(overrides)
    return build_factors(**arguments)


def test_a_tsb_forecast_is_explained_as_declining_demand_not_as_the_usual_method():
    factors = _normal_factors(forecast_model="TSB")
    labels = [factor.label for factor in factors]
    assert "Declining demand" in labels and "How demand was projected" not in labels
    text = " ".join(factor.detail for factor in factors)
    assert "has been falling" in text and "TSB" not in text  # plain words, no model name


def test_any_other_forecast_model_keeps_the_usual_explanation():
    for model in (None, "SES", "SBA"):
        labels = [factor.label for factor in _normal_factors(forecast_model=model)]
        assert "How demand was projected" in labels and "Declining demand" not in labels

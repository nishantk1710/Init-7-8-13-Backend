"""OAR Max stock consistency floor (oar-similarity-6).

The three weighted averages are independent, so the Max can come out at or
below the reorder point. After all three are calculated and rounded:

    Max_final = max( Max_weighted , ROP_final + SS_final )

only when all three are present; ROP and safety stock are never changed; the
trace keeps the original beside the floor and the final value.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.initiatives.i7.inventory import max_stock as max_stock_module
from app.initiatives.i7.oar import estimate
from app.initiatives.i7.oar.estimate import MAX_FLOOR_REASON, apply_max_floor
from app.initiatives.i7.oar.types import ALGORITHM_VERSION, EstimateStatus
from app.initiatives.i7.recommendations.builder import _max_floor_entries
from tests.i7.test_oar_similarity import neighbour_with


def calculate(*neighbours):
    return estimate.calculate(list(neighbours), service_level_configured=True)


def only(ss: int, rop: int, mx: int):
    """One neighbour whose values are exactly the three figures, so the weighted estimate is them."""
    return calculate(neighbour_with("0.90", ss, rop, mx))


# --- 1-3. below / equal / above the floor ---------------------------------------


def test_max_below_the_floor_is_raised():
    """The motivating case: ROP 2, safety stock 1, weighted Max 2 -> Max 3."""
    result = only(ss=1, rop=2, mx=2)
    assert result.status is EstimateStatus.SUCCESS
    assert (result.rop, result.safety_stock, result.max_stock) == (2, 1, 3)


def test_max_equal_to_the_floor_is_unchanged():
    result = only(ss=1, rop=2, mx=3)
    assert (result.rop, result.safety_stock, result.max_stock) == (2, 1, 3)
    assert dict(result.trace)["max_stock_floor_applied"] == "false"


def test_max_above_the_floor_is_unchanged():
    result = only(ss=1, rop=2, mx=10)
    assert result.max_stock == 10
    assert dict(result.trace)["max_stock_floor_applied"] == "false"


def test_rop_and_safety_stock_are_never_changed_by_the_floor():
    raised = only(ss=4, rop=23, mx=2)
    assert (raised.rop, raised.safety_stock) == (23, 4)
    assert raised.max_stock == 27


def test_the_floor_is_applied_after_rounding_not_before():
    """Two neighbours: ROP averages 1.5 -> 2, SS averages 0.5 -> 1, Max averages 1.5 -> 2.
    The floor is 2 + 1 = 3 on the *rounded* values (not 1.5 + 0.5 = 2)."""
    result = calculate(neighbour_with("0.90", 0, 1, 1), neighbour_with("0.90", 1, 2, 2))
    assert (result.rop, result.safety_stock) == (2, 1)
    assert result.max_stock == 3


# --- 4. zero values --------------------------------------------------------------


def test_all_zero_values_stay_zero():
    """Zero is a present value: floor 0 + 0 = 0, so a Max of 0 is not raised."""
    result = only(ss=0, rop=0, mx=0)
    assert (result.rop, result.safety_stock, result.max_stock) == (0, 0, 0)
    assert dict(result.trace)["max_stock_floor_applied"] == "false"


def test_a_zero_max_is_raised_when_rop_and_safety_stock_are_positive():
    assert only(ss=1, rop=2, mx=0).max_stock == 3


# --- 5. missing inputs keep the existing behaviour ---------------------------------


@pytest.mark.parametrize(
    "ss, rop, mx",
    [(None, 2, 2), (1, None, 2), (1, 2, None), (None, None, None)],
)
def test_a_missing_value_is_never_filled_in(ss, rop, mx):
    result = only(ss=ss, rop=rop, mx=mx)
    assert result.status is EstimateStatus.SUCCESS
    assert result.max_stock == mx  # unchanged: a missing Max stays missing
    assert (result.rop, result.safety_stock) == (rop, ss)
    trace = dict(result.trace)
    assert trace["max_stock_floor_applied"] == "false"
    assert trace["max_stock_floor_reason"] == "NOT_EVALUATED_MISSING_INPUT"
    assert "max_stock_floor" not in trace and "max_stock_final" not in trace


def test_apply_max_floor_returns_none_for_none():
    assert apply_max_floor(2, 1, None)[0] is None
    assert apply_max_floor(None, 1, 2)[0] == 2
    assert apply_max_floor(2, None, 2)[0] == 2


def test_insufficient_neighbours_still_produces_no_numbers():
    result = estimate.calculate(
        [neighbour_with("0.90", 1, 2, 2)], service_level_configured=True, minimum_neighbours=3
    )
    assert result.status is EstimateStatus.NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS
    assert result.max_stock is None and result.rop is None and result.safety_stock is None


def test_no_contributing_neighbour_still_produces_no_numbers():
    result = calculate(neighbour_with("0.90", 1, 2, 2, eligible=False))
    assert result.status is not EstimateStatus.SUCCESS
    assert result.max_stock is None


# --- 6. the audit trace -------------------------------------------------------------


def test_the_trace_records_original_floor_final_and_whether_it_was_applied():
    trace = dict(only(ss=1, rop=2, mx=2).trace)
    assert trace["max_stock_weighted"] == "2"
    assert trace["max_stock_floor"] == "3"
    assert trace["max_stock_final"] == "3"
    assert trace["max_stock_floor_applied"] == "true"
    assert trace["max_stock_floor_reason"] == MAX_FLOOR_REASON == "MAX_RAISED_TO_ROP_PLUS_SAFETY_STOCK"


def test_the_original_weighted_max_is_not_overwritten_in_the_trace():
    """The raw average is still there, next to the adjusted result."""
    result = only(ss=1, rop=2, mx=2)
    trace = dict(result.trace)
    assert trace["max_stock_weighted"] == "2" and result.max_stock == 3
    assert "max_stock_raw" in trace  # the unrounded weighted average


def test_an_unadjusted_result_says_so():
    trace = dict(only(ss=1, rop=2, mx=10).trace)
    assert trace["max_stock_floor_applied"] == "false"
    assert trace["max_stock_floor_reason"] == "NOT_NEEDED"
    assert trace["max_stock_final"] == trace["max_stock_weighted"] == "10"


def test_the_floor_entries_come_before_the_per_field_arithmetic():
    """The stored trace is cut at a fixed length; the audit entries must outlive that."""
    keys = [key for key, _ in only(ss=1, rop=2, mx=2).trace]
    assert keys.index("max_stock_floor_applied") < keys.index("safety_stock_numerator")


def test_the_recommendation_trace_carries_the_floor_entries():
    stored = ";".join(f"{key}={value}" for key, value in only(ss=1, rop=2, mx=2).trace)
    entries = dict(_max_floor_entries(stored))
    assert entries == {
        "max_stock_weighted": "2",
        "max_stock_floor": "3",
        "max_stock_final": "3",
        "max_stock_floor_applied": "true",
        "max_stock_floor_reason": MAX_FLOOR_REASON,
    }


def test_a_result_stored_before_the_floor_gets_no_invented_entries():
    assert _max_floor_entries(None) == []
    assert _max_floor_entries("") == []
    assert _max_floor_entries("eligible_neighbours=4;rop_raw=1.5;rounding_method=CEILING") == []


def test_the_floor_entries_survive_the_stored_trace_length_limit():
    """i7_oar_target.estimate_trace holds 1000 characters; the entries are within them."""
    worst = only(ss=1, rop=2, mx=2)
    stored = ";".join(f"{key}={value}" for key, value in worst.trace)[:1000]
    assert "max_stock_floor_reason=MAX_RAISED_TO_ROP_PLUS_SAFETY_STOCK" in stored


# --- 7. stock items are unaffected --------------------------------------------------


def test_the_stock_item_max_strategies_do_not_use_the_oar_floor():
    """Review period: Max = ROP + demand x T, from its own inputs -- and it can sit below ROP + SS
    if its inputs say so; nothing here raises it."""
    context = max_stock_module.MaxStockContext(
        safety_stock=5, rop=10, forecast_rate=Decimal("1"), criticality="NORMAL",
        unit_price=None, ordering_cost=None, holding_cost_rate=None, review_period_months=Decimal("1"),
    )
    result = max_stock_module.ReviewPeriodMaxStockStrategy().calculate(context)
    assert result.max_stock == 11  # ROP 10 + 1 x 1, below ROP + SS = 15, left alone
    assert "max_stock_floor_applied" not in dict(result.trace)


def test_the_not_configured_strategy_still_returns_no_number():
    context = max_stock_module.MaxStockContext(
        safety_stock=5, rop=10, forecast_rate=None, criticality=None,
        unit_price=None, ordering_cost=None, holding_cost_rate=None, review_period_months=None,
    )
    assert max_stock_module.NotConfiguredMaxStockStrategy().calculate(context).max_stock is None


def test_the_floor_lives_only_in_the_oar_estimate():
    import inspect

    assert "apply_max_floor" not in inspect.getsource(max_stock_module)


# --- 8. existing similarity behaviour is unchanged -----------------------------------


def test_the_weighted_averages_are_unchanged_when_no_floor_is_needed():
    """The documented example: (0.90x10 + 0.80x20) / 1.7 -> SS 15, ROP 30, Max 45 (all above the floor)."""
    result = calculate(neighbour_with("0.90", 10, 20, 30), neighbour_with("0.80", 20, 40, 60))
    assert (result.safety_stock, result.rop, result.max_stock) == (15, 30, 45)
    trace = dict(result.trace)
    assert abs(float(trace["safety_stock_raw"]) - 14.7058823529) < 1e-6
    assert trace["max_stock_floor_applied"] == "false"


def test_the_algorithm_version_records_the_floor():
    assert int(ALGORITHM_VERSION.rsplit("-", 1)[1]) >= 6  # the floor arrived in 6; later versions keep it


def test_the_recommendation_builder_ignores_a_row_without_a_trace():
    from app.initiatives.i7.recommendations import builder

    row = SimpleNamespace()
    assert getattr(row, "estimate_trace", None) is None
    assert builder._max_floor_entries(getattr(row, "estimate_trace", None)) == []


# --- the approver's view: the recommendation's own trace -----------------------------


def _built(estimate_trace):
    from app.initiatives.i7.policy import PolicyDocument
    from app.initiatives.i7.recommendations import builder
    from tests.i7.test_recommendation_detail_completeness import oar_row

    row = oar_row(
        estimate_status="SUCCESS", oar_safety_stock=1, oar_rop=2, oar_max_stock=3,
        estimate_trace=estimate_trace,
    )
    return builder.build_oar_recommendation(row, PolicyDocument())


def test_an_approver_sees_the_original_and_the_adjusted_max_in_the_recommendation_trace():
    stored = ";".join(f"{key}={value}" for key, value in only(ss=1, rop=2, mx=2).trace)
    trace = dict(_built(stored).calculation_trace)
    assert trace["max_stock"] == "3"  # the figure recommended
    assert trace["max_stock_weighted"] == "2"  # what the neighbours' average said
    assert trace["max_stock_floor"] == "3"
    assert trace["max_stock_floor_applied"] == "true"
    assert trace["max_stock_floor_reason"] == MAX_FLOOR_REASON


def test_a_recommendation_from_an_older_oar_run_has_no_floor_entries():
    trace = dict(_built("eligible_neighbours=4;rounding_method=CEILING").calculation_trace)
    assert "max_stock_floor_applied" not in trace
    assert trace["max_stock"] == "3"

"""The declining-demand rule, v1 -- every condition, boundary and data problem.

Pure: the rule takes months and a policy. Nothing here touches a database.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.initiatives.i7.features.declining import (
    DecliningStatus,
    evaluate,
    evaluate_dense_series,
)
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.thresholds import DecliningDemandPolicy

POLICY = DecliningDemandPolicy()


def months(values, start=(2025, 1)):
    """Consecutive first-of-month points for ``values`` (None = unknown)."""
    year, month = start
    points = []
    for value in values:
        points.append((date(year, month, 1), None if value is None else Decimal(str(value))))
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return points


def status_of(values, policy=POLICY):
    return evaluate(months(values), policy).status


# The task's worked example: 3 of the first 6 months, 1 of the last 6, last 3 empty.
EXAMPLE = [10, 0, 8, 0, 6, 0, 0, 5, 0, 0, 0, 0]


# --- the declining case ------------------------------------------------------


def test_the_worked_example_is_flagged_with_the_numbers_behind_it():
    result = evaluate(months(EXAMPLE), POLICY)
    assert result.status is DecliningStatus.DECLINING_DEMAND and result.flagged
    assert (result.nonzero_months, result.early_nonzero, result.recent_nonzero) == (4, 3, 1)
    assert result.early_rate == Decimal("0.5")
    assert round(result.recent_rate, 4) == Decimal("0.1667")
    assert round(result.decline_pct, 4) == Decimal("0.6667")
    assert result.latest_months_zero is True
    assert (result.window_start, result.window_end) == (date(2025, 1, 1), date(2025, 12, 1))
    assert result.months_evaluated == 12 and result.rule_version == "declining-v1"
    assert "67%" in result.reason and "latest 3 months are empty" in result.reason


def test_a_decline_of_exactly_half_is_flagged():
    # early 4 of 6, recent 2 of 6 -> 1 - (2/6)/(4/6) = 0.5 exactly.
    assert status_of([5, 5, 5, 5, 0, 0, 5, 5, 0, 0, 0, 0]) is DecliningStatus.DECLINING_DEMAND


# --- each condition on its own -----------------------------------------------


def test_fewer_than_twelve_complete_months_is_insufficient_history_not_zero_filled():
    result = evaluate(months(EXAMPLE[:11]), POLICY)
    assert result.status is DecliningStatus.INSUFFICIENT_HISTORY
    assert result.months_evaluated == 11 and "11 complete months" in result.reason


def test_fewer_than_four_months_with_demand_is_insufficient_history():
    assert status_of([5, 0, 5, 0, 0, 0, 0, 0, 0, 0, 0, 0]) is DecliningStatus.INSUFFICIENT_HISTORY
    assert status_of([0] * 12) is DecliningStatus.INSUFFICIENT_HISTORY


def test_fewer_than_three_early_months_with_demand_is_not_flagged():
    result = evaluate(months([5, 5, 0, 0, 0, 0, 5, 5, 0, 0, 0, 0]), POLICY)
    assert result.status is DecliningStatus.INSUFFICIENT_EARLY_DEMAND
    assert result.early_nonzero == 2


def test_a_decline_below_half_is_not_flagged():
    # early 3 of 6, recent 2 of 6 -> 33 %.
    result = evaluate(months([5, 5, 5, 0, 0, 0, 5, 5, 0, 0, 0, 0]), POLICY)
    assert result.status is DecliningStatus.DECLINE_THRESHOLD_NOT_MET
    assert round(result.decline_pct, 2) == Decimal("0.33")


def test_demand_in_a_month_that_is_not_falling_at_all_is_not_flagged():
    assert status_of([5, 5, 5, 0, 0, 0, 5, 5, 5, 0, 0, 0]) is DecliningStatus.DECLINE_THRESHOLD_NOT_MET


@pytest.mark.parametrize("position", [9, 10, 11])
def test_demand_in_any_of_the_latest_three_months_is_not_flagged(position):
    values = [10, 0, 8, 0, 6, 0, 0, 0, 0, 0, 0, 0]
    values[position] = 5
    result = evaluate(months(values), POLICY)
    assert result.status is DecliningStatus.RECENT_DEMAND_PRESENT
    assert result.latest_months_zero is False and not result.flagged


def test_the_conditions_are_checked_in_a_fixed_order_so_the_reason_is_deterministic():
    # Fails both the early-demand and the recent-demand conditions: the earlier
    # one in the documented order is the reason given.
    assert status_of([5, 5, 0, 0, 0, 0, 0, 0, 0, 0, 0, 5]) is DecliningStatus.INSUFFICIENT_HISTORY


# --- missing data is never zero ------------------------------------------------


def test_a_gap_in_the_months_is_not_evaluable_and_not_filled_with_zeros():
    points = months(EXAMPLE)
    del points[5]
    result = evaluate(points, POLICY)
    assert result.status is DecliningStatus.DATA_NOT_EVALUABLE
    assert "not consecutive" in result.reason


def test_an_unknown_month_is_not_evaluable_and_not_counted_as_zero():
    values = list(EXAMPLE)
    values[3] = None
    result = evaluate(months(values), POLICY)
    assert result.status is DecliningStatus.DATA_NOT_EVALUABLE and "unknown" in result.reason


def test_no_months_at_all_is_not_evaluable():
    assert evaluate([], POLICY).status is DecliningStatus.DATA_NOT_EVALUABLE


def test_only_the_latest_twelve_months_count_when_there_is_more_history():
    older = [50, 50, 50]  # months before the window: must not influence the result
    result = evaluate(months(older + EXAMPLE, start=(2024, 10)), POLICY)
    assert result.status is DecliningStatus.DECLINING_DEMAND
    assert (result.window_start, result.window_end) == (date(2025, 1, 1), date(2025, 12, 1))
    assert result.months_evaluated == 12


def test_a_negative_or_zero_quantity_is_not_demand():
    values = [-4 if v == 0 else v for v in EXAMPLE]
    assert status_of(values) is DecliningStatus.DECLINING_DEMAND


# --- the latest staged month is treated as incomplete --------------------------


def observation(period, quantity):
    return SimpleNamespace(period=period, quantity=Decimal(str(quantity)))


def dense(values, start=(2025, 1)):
    return [observation(p, q) for p, q in months(values, start)]


def test_the_latest_staged_month_is_ignored_even_when_it_has_demand():
    observations = dense(EXAMPLE + [9])  # 13 staged months; the last may be partial
    result = evaluate_dense_series(observations, POLICY)
    assert result.status is DecliningStatus.DECLINING_DEMAND
    assert result.window_end == date(2025, 12, 1)  # not January 2026


def test_twelve_staged_months_are_not_enough_because_the_last_is_dropped():
    result = evaluate_dense_series(dense(EXAMPLE), POLICY)
    assert result.status is DecliningStatus.INSUFFICIENT_HISTORY
    assert "11 complete months" in result.reason


def test_a_series_with_no_months_is_not_evaluable():
    assert evaluate_dense_series([], POLICY).status is DecliningStatus.DATA_NOT_EVALUABLE


# --- zero early demand does not divide by zero -----------------------------------


def test_a_zero_early_rate_gives_a_status_not_an_error():
    lenient = DecliningDemandPolicy(minimum_early_nonzero_months=1)
    result = evaluate(months([0, 0, 0, 0, 0, 0, 5, 5, 5, 5, 0, 0]), lenient)
    assert result.status is DecliningStatus.INSUFFICIENT_EARLY_DEMAND
    assert result.early_rate == 0 and result.decline_pct is None


# --- repeatability and refresh --------------------------------------------------


def test_the_same_input_gives_the_same_result():
    assert evaluate(months(EXAMPLE), POLICY) == evaluate(months(EXAMPLE), POLICY)


def test_a_flag_clears_when_demand_resumes():
    flagged = evaluate(months(EXAMPLE), POLICY)
    assert flagged.flagged
    # Two more months arrive and there is demand again. The window moves on, so
    # the earlier half changes as well; either way the new evaluation does not
    # flag it, and it is the next feature run's row that replaces the old flag.
    resumed = evaluate(months(EXAMPLE + [7, 6]), POLICY)
    assert not resumed.flagged
    assert resumed.status is DecliningStatus.DECLINE_THRESHOLD_NOT_MET
    # And with demand in the very latest months the reason is that one.
    assert evaluate(months([10, 0, 8, 0, 6, 0, 0, 0, 0, 0, 0, 5]), POLICY).status is (
        DecliningStatus.RECENT_DEMAND_PRESENT
    )


# --- the policy ---------------------------------------------------------------


def test_the_thresholds_come_from_the_policy_not_the_code():
    strict = DecliningDemandPolicy(minimum_decline=0.8)
    assert status_of(EXAMPLE, strict) is DecliningStatus.DECLINE_THRESHOLD_NOT_MET  # 67 % < 80 %
    assert evaluate(months(EXAMPLE), strict).rule_version == "declining-v1"
    custom = DecliningDemandPolicy(rule_version="declining-v2")
    assert evaluate(months(EXAMPLE), custom).rule_version == "declining-v2"


def test_the_policy_document_carries_the_rule_with_its_defaults():
    rule = PolicyDocument().declining_demand
    assert (rule.window_months, rule.minimum_nonzero_months, rule.minimum_early_nonzero_months) == (12, 4, 3)
    assert (rule.minimum_decline, rule.zero_latest_months, rule.rule_version) == (0.5, 3, "declining-v1")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window_months": 11},  # odd: cannot split into equal halves
        {"zero_latest_months": 7},  # more than half the window
        {"minimum_early_nonzero_months": 7},
        {"minimum_nonzero_months": 13},
        {"minimum_decline": 0},
        {"minimum_decline": 1.5},
    ],
)
def test_an_inconsistent_policy_is_refused(kwargs):
    with pytest.raises(ValueError):
        DecliningDemandPolicy(**kwargs)

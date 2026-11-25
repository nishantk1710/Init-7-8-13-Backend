"""Consumption metrics, the history span and the calculation anchor for Min-Max -> OAR."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.initiatives.i7.policy.reverse_conversion import current_reverse_conversion_policy
from app.initiatives.i7.reverse_conversion.errors import AnchorError
from app.initiatives.i7.reverse_conversion.metrics import (
    add_months,
    compute_consumption_metrics,
    history_span_months,
    months_between,
    resolve_anchor,
)
from app.initiatives.i7.reverse_conversion.types import MonthlyMovement

POLICY = current_reverse_conversion_policy()
ANCHOR = date(2026, 10, 1)


def mv(year: int, month: int, quantity: float = 5, issues: int = 1, reversals: int = 0, day: int = 1) -> MonthlyMovement:
    return MonthlyMovement(date(year, month, day), Decimal(str(quantity)), issues, reversals)


# --- History span: the approved formula and its examples ----------------------------------


@pytest.mark.parametrize(
    ("first", "last", "span", "passes"),
    [
        (date(2024, 12, 1), date(2025, 11, 1), 12, True),
        (date(2025, 1, 1), date(2025, 11, 1), 11, False),
        (date(2024, 12, 1), date(2026, 11, 1), 24, True),
        (date(2025, 6, 1), date(2025, 11, 1), 6, False),
    ],
)
def test_history_span_matches_the_approved_examples(first, last, span, passes):
    assert history_span_months(first, last) == span
    assert (span >= POLICY.minimum_history_span_months) is passes


def test_history_span_boundary_is_eleven_versus_twelve():
    assert history_span_months(date(2025, 11, 1), date(2026, 9, 1)) == 11
    assert history_span_months(date(2025, 10, 1), date(2026, 9, 1)) == 12


def test_a_single_movement_month_has_a_span_of_one():
    assert history_span_months(date(2026, 3, 1), date(2026, 3, 1)) == 1


def test_history_span_is_undefined_without_a_valid_movement():
    assert history_span_months(None, None) is None
    assert history_span_months(date(2026, 1, 1), None) is None


def test_the_span_counts_months_without_consumption_between_the_ends():
    # Two movement months 20 months apart: a span of 21, even though only two months had use.
    metrics = compute_consumption_metrics([mv(2023, 1), mv(2024, 9)], ANCHOR, POLICY)
    assert metrics.history_span_months == 21
    assert metrics.lifetime_months_with_use == 2


def test_the_span_is_measured_up_to_the_anchor_not_to_a_later_movement():
    metrics = compute_consumption_metrics([mv(2025, 10), mv(2026, 11)], ANCHOR, POLICY)
    assert metrics.last_valid_month == date(2025, 10, 1)
    assert metrics.history_span_months == 1


# --- Valid movement ----------------------------------------------------------------------


def test_only_a_month_with_net_issued_quantity_above_zero_is_a_valid_movement():
    movements = [
        mv(2026, 5, quantity=3),  # valid
        mv(2026, 6, quantity=0),  # no net quantity
        mv(2026, 7, quantity=-2, issues=0, reversals=1),  # reversal only
    ]
    metrics = compute_consumption_metrics(movements, ANCHOR, POLICY)
    assert metrics.months_with_use_latest_window == 1
    assert metrics.first_valid_month == metrics.last_valid_month == date(2026, 5, 1)
    assert metrics.lifetime_months_with_use == 1


def test_rows_of_one_month_are_added_before_the_month_is_judged():
    # 5 issued then 5 reversed in the same month nets to zero: not a movement.
    cancelled = compute_consumption_metrics([mv(2026, 5, 5, 1, 0), mv(2026, 5, -5, 0, 1)], ANCHOR, POLICY)
    assert cancelled.months_with_use_latest_window == 0
    # Two partial rows that add up to a positive month are one valid movement month.
    summed = compute_consumption_metrics([mv(2026, 5, 2), mv(2026, 5, 3, day=15)], ANCHOR, POLICY)
    assert summed.months_with_use_latest_window == 1
    assert summed.issues_latest_window == 2


# --- Windows -----------------------------------------------------------------------------


def test_latest_window_is_the_twelve_months_ending_at_the_anchor_inclusive():
    movements = [mv(2025, 10), mv(2025, 11), mv(2026, 10)]  # Oct 2025 is the 13th month back
    metrics = compute_consumption_metrics(movements, ANCHOR, POLICY)
    assert metrics.months_with_use_latest_window == 2  # Nov 2025 and Oct 2026
    assert metrics.issues_latest_window == 2
    assert metrics.months_with_use_prior_window == 1  # Oct 2025
    assert metrics.issues_prior_window == 1


def test_prior_window_is_the_twelve_months_before_the_latest_window():
    movements = [mv(2024, 10), mv(2024, 11), mv(2025, 10)]  # Oct 2024 is the 25th month back
    metrics = compute_consumption_metrics(movements, ANCHOR, POLICY)
    assert metrics.months_with_use_prior_window == 2  # Nov 2024 and Oct 2025
    assert metrics.issues_prior_window == 2
    assert metrics.months_with_use_latest_window == 0
    assert metrics.lifetime_months_with_use == 3  # lifetime still sees Oct 2024


def test_net_issues_subtract_reversals_and_floor_at_zero():
    metrics = compute_consumption_metrics([mv(2026, 5, quantity=4, issues=4, reversals=1)], ANCHOR, POLICY)
    assert metrics.issues_latest_window == 3
    floored = compute_consumption_metrics([mv(2026, 5, quantity=1, issues=1, reversals=3)], ANCHOR, POLICY)
    assert floored.issues_latest_window == 0


def test_issue_transactions_count_separately_from_months():
    # Four issues in one month: four transactions, one month with use.
    metrics = compute_consumption_metrics([mv(2026, 3, quantity=8, issues=4)], ANCHOR, POLICY)
    assert metrics.issues_latest_window == 4
    assert metrics.months_with_use_latest_window == 1


def test_months_since_last_movement_and_material_age():
    metrics = compute_consumption_metrics([mv(2025, 3), mv(2026, 8)], ANCHOR, POLICY)
    assert metrics.months_since_last_movement == 2  # Aug -> Oct 2026
    assert metrics.material_age_months == 20  # Mar 2025 .. Oct 2026 inclusive
    in_anchor_month = compute_consumption_metrics([mv(2026, 10)], ANCHOR, POLICY)
    assert in_anchor_month.months_since_last_movement == 0


def test_a_material_that_never_moved_has_no_dates_and_no_span():
    metrics = compute_consumption_metrics([], ANCHOR, POLICY)
    assert metrics.first_valid_month is None and metrics.last_valid_month is None
    assert metrics.history_span_months is None
    assert metrics.months_since_last_movement is None
    assert metrics.material_age_months is None
    assert metrics.lifetime_months_with_use == 0
    assert metrics.issues_latest_window == metrics.issues_prior_window == 0


# --- The inconsistency the rule exists for -------------------------------------------------


def test_issue_count_and_months_can_disagree_in_the_latest_window():
    # Issues and reversals cancel in the count, but the quantity still netted positive.
    zero_issues = compute_consumption_metrics([mv(2026, 5, quantity=2, issues=1, reversals=1)], ANCHOR, POLICY)
    assert zero_issues.issues_latest_window == 0 and zero_issues.months_with_use_latest_window == 1
    # Issues counted, but the quantity netted to nothing.
    no_month = compute_consumption_metrics([mv(2026, 5, quantity=0, issues=2, reversals=0)], ANCHOR, POLICY)
    assert no_month.issues_latest_window == 2 and no_month.months_with_use_latest_window == 0


# --- Partial month -----------------------------------------------------------------------


def test_movements_after_the_anchor_are_ignored_not_counted_as_a_low_month():
    movements = [mv(2026, 8), mv(2026, 11, quantity=9, issues=9)]  # November is the partial month
    metrics = compute_consumption_metrics(movements, ANCHOR, POLICY)
    assert metrics.issues_latest_window == 1
    assert metrics.last_valid_month == date(2026, 8, 1)
    assert metrics.lifetime_months_with_use == 1


def test_a_material_whose_only_movement_is_after_the_anchor_has_not_moved_yet():
    metrics = compute_consumption_metrics([mv(2026, 11)], ANCHOR, POLICY)
    assert metrics.lifetime_months_with_use == 0 and metrics.history_span_months is None


def test_any_day_of_the_anchor_month_is_normalised_to_its_first():
    metrics = compute_consumption_metrics([mv(2026, 10)], date(2026, 10, 27), POLICY)
    assert metrics.anchor == date(2026, 10, 1)
    assert metrics.months_with_use_latest_window == 1


# --- Calendar helpers --------------------------------------------------------------------


def test_add_months_and_months_between_cross_year_ends():
    assert add_months(date(2026, 1, 1), -1) == date(2025, 12, 1)
    assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 1)
    assert months_between(date(2025, 12, 1), date(2026, 2, 1)) == 2
    assert months_between(date(2026, 3, 5), date(2026, 3, 28)) == 0


# --- The anchor ---------------------------------------------------------------------------


def loaded(year, month, day, hour=12, tz=timezone.utc):
    return datetime(year, month, day, hour, 0, tzinfo=tz)


def test_the_anchor_is_the_month_before_when_the_latest_month_had_not_ended_at_load():
    resolution = resolve_anchor(latest_staged_month=date(2026, 11, 1), data_loaded_at=loaded(2026, 11, 23), configured_anchor=None)
    assert resolution.month == date(2026, 10, 1)
    assert resolution.source == "data_load_timestamp"
    assert resolution.warnings == ()


def test_the_anchor_is_the_latest_month_once_it_had_ended_before_the_load():
    resolution = resolve_anchor(latest_staged_month=date(2026, 11, 1), data_loaded_at=loaded(2026, 12, 1), configured_anchor=None)
    assert resolution.month == date(2026, 11, 1)


def test_the_load_day_itself_does_not_complete_its_month():
    # Loaded on 30 November: November has not ended yet.
    resolution = resolve_anchor(latest_staged_month=date(2026, 11, 1), data_loaded_at=loaded(2026, 11, 30, hour=23), configured_anchor=None)
    assert resolution.month == date(2026, 10, 1)


def test_the_load_date_is_taken_in_utc():
    # 23:30 on 30 Nov at UTC-5 is 04:30 on 1 Dec UTC, so November had ended.
    local = timezone(timedelta(hours=-5))
    resolution = resolve_anchor(
        latest_staged_month=date(2026, 11, 1), data_loaded_at=datetime(2026, 11, 30, 23, 30, tzinfo=local), configured_anchor=None
    )
    assert resolution.month == date(2026, 11, 1)


def test_without_a_load_timestamp_and_without_a_configured_anchor_it_refuses_to_guess():
    with pytest.raises(AnchorError, match="Configure the anchor explicitly"):
        resolve_anchor(latest_staged_month=date(2026, 11, 1), data_loaded_at=None, configured_anchor=None)


def test_no_staged_consumption_means_no_anchor():
    with pytest.raises(AnchorError):
        resolve_anchor(latest_staged_month=None, data_loaded_at=loaded(2026, 11, 23), configured_anchor=date(2026, 10, 1))


def test_a_configured_anchor_is_accepted_when_it_was_a_complete_month():
    resolution = resolve_anchor(latest_staged_month=date(2026, 11, 1), data_loaded_at=loaded(2026, 11, 23), configured_anchor=date(2026, 10, 15))
    assert resolution.month == date(2026, 10, 1)  # normalised to the first
    assert resolution.source == "configured"


def test_a_configured_anchor_for_an_incomplete_month_is_refused():
    with pytest.raises(AnchorError, match="not a complete month"):
        resolve_anchor(latest_staged_month=date(2026, 11, 1), data_loaded_at=loaded(2026, 11, 23), configured_anchor=date(2026, 11, 1))


def test_a_configured_anchor_after_the_latest_staged_month_is_refused():
    with pytest.raises(AnchorError, match="after the latest staged"):
        resolve_anchor(latest_staged_month=date(2026, 10, 1), data_loaded_at=loaded(2026, 12, 5), configured_anchor=date(2026, 11, 1))


def test_a_configured_anchor_without_a_timestamp_is_accepted_with_a_warning():
    resolution = resolve_anchor(latest_staged_month=date(2026, 11, 1), data_loaded_at=None, configured_anchor=date(2026, 10, 1))
    assert resolution.month == date(2026, 10, 1)
    assert any("could not be checked" in warning for warning in resolution.warnings)


def test_stale_data_is_flagged_without_failing():
    # Latest staged month is August but the load was in November: derive August, and warn.
    resolution = resolve_anchor(latest_staged_month=date(2026, 8, 1), data_loaded_at=loaded(2026, 11, 23), configured_anchor=None)
    assert resolution.month == date(2026, 8, 1)
    assert any("stale" in warning for warning in resolution.warnings)

"""SOP 3.1.1 consumption count -- MSEG transaction count, not non-zero months.

The clarified requirement: "more than four consumptions in the trailing
twelve months (MSEG issues)". consumption_count_12m must be a transaction-
level count (issues minus reversals) over a trailing 12-month window ending
at the extract's own last staged month -- never a count of non-zero months
(non_zero_periods), which continues to feed ADI/CV-squared only and is a
materially different metric.
"""

from datetime import date

from app.initiatives.i7.features.builder import _months_back, consumption_count, consumption_count_12m


def month(year: int, m: int) -> date:
    return date(year, m, 1)


# --- The essential distinction: transactions, not non-zero months -----------


def test_multiple_transactions_in_the_same_month_all_count():
    """January=2, February=1, March=3, April=1 -> count=7, not 4 non-zero
    months. This is the test that proves we are no longer counting months."""
    rows = [
        (month(2026, 1), 2, 0),
        (month(2026, 2), 1, 0),
        (month(2026, 3), 3, 0),
        (month(2026, 4), 1, 0),
    ]
    assert consumption_count_12m(rows, reference=month(2026, 4)) == 7


def test_zero_consumption_gives_count_zero():
    assert consumption_count_12m([], reference=month(2026, 6)) == 0
    assert consumption_count_12m(None, reference=month(2026, 6)) == 0


def test_no_reference_date_gives_count_zero():
    """No extract window means no defined trailing period."""
    rows = [(month(2026, 1), 5, 0)]
    assert consumption_count_12m(rows, reference=None) == 0


# --- Threshold semantics (paired with the conversion trigger's own >4) ------


def test_five_issue_transactions_yields_count_five():
    rows = [(month(2026, 1), 5, 0)]
    assert consumption_count_12m(rows, reference=month(2026, 1)) == 5


def test_four_issue_transactions_yields_count_four():
    rows = [(month(2026, 1), 4, 0)]
    assert consumption_count_12m(rows, reference=month(2026, 1)) == 4


def test_ten_issue_transactions_yields_count_ten():
    rows = [(month(2026, 1), 10, 0)]
    assert consumption_count_12m(rows, reference=month(2026, 1)) == 10


# --- Trailing 12-month window -----------------------------------------------


def test_issue_outside_the_trailing_window_is_excluded():
    """13 months before the reference month is outside the trailing 12."""
    rows = [(month(2025, 1), 5, 0)]
    assert consumption_count_12m(rows, reference=month(2026, 3)) == 0


def test_issue_inside_the_trailing_window_is_included():
    rows = [(month(2025, 4), 5, 0)]
    assert consumption_count_12m(rows, reference=month(2026, 3)) == 5


def test_issue_after_the_reference_date_is_excluded():
    rows = [(month(2026, 6), 5, 0)]
    assert consumption_count_12m(rows, reference=month(2026, 3)) == 0


def test_exactly_twelve_months_are_represented():
    """The window is [reference - 11 months, reference] inclusive -- 12
    months total, matching "trailing twelve months"."""
    assert _months_back(month(2026, 12), 11) == month(2026, 1)
    rows = [(month(2026, m), 1, 0) for m in range(1, 13)]
    assert consumption_count_12m(rows, reference=month(2026, 12)) == 12

    # A 13th month, one earlier, must not be included.
    rows_with_extra = rows + [(month(2025, 12), 100, 0)]
    assert consumption_count_12m(rows_with_extra, reference=month(2026, 12)) == 12


# --- Reversal netting --------------------------------------------------------


def test_reversals_are_netted_against_issues_in_the_same_month():
    """3 issues, 1 reversal (202/262) in the same month -> net count 2."""
    rows = [(month(2026, 1), 3, 1)]
    assert consumption_count_12m(rows, reference=month(2026, 1)) == 2


def test_reversals_across_months_net_within_the_window():
    rows = [(month(2026, 1), 5, 0), (month(2026, 2), 0, 2)]
    assert consumption_count_12m(rows, reference=month(2026, 2)) == 3


def test_net_count_floors_at_zero_not_negative():
    """More reversals than issues (e.g. the matching issue predates the
    extract) must not make the trailing count negative."""
    rows = [(month(2026, 1), 1, 5)]
    assert consumption_count_12m(rows, reference=month(2026, 1)) == 0


# --- The window is a parameter: 6 months since 2026-10-22 ---------------------------------------------


def test_a_six_month_window_ends_at_the_reference_and_starts_five_months_back():
    rows = [
        (month(2025, 7), 3, 0),  # seventh month back: outside a 6-month window, inside a 12-month one
        (month(2025, 8), 2, 0),  # sixth month back: the first month inside
        (month(2026, 1), 3, 1),  # the reference month: 2 net
    ]

    assert consumption_count(rows, month(2026, 1), 6) == 4
    assert consumption_count(rows, month(2026, 1), 12) == 7


def test_the_twelve_month_function_is_the_twelve_month_window():
    rows = [(month(2025, 2), 5, 0), (month(2026, 1), 1, 0)]

    assert consumption_count_12m(rows, month(2026, 1)) == consumption_count(rows, month(2026, 1), 12) == 6


def test_a_six_month_window_nets_reversals_and_never_goes_negative():
    assert consumption_count([(month(2026, 1), 1, 3)], month(2026, 1), 6) == 0
    assert consumption_count([(month(2025, 11), 6, 1), (month(2026, 1), 1, 0)], month(2026, 1), 6) == 6


def test_more_than_four_in_six_months_is_the_usage_trigger():
    """Five net issues in six months passes the rule (> 4); four does not."""
    five = [(month(2025, 10), 2, 0), (month(2025, 12), 3, 0)]
    four = [(month(2025, 10), 2, 0), (month(2025, 12), 2, 0)]

    assert consumption_count(five, month(2026, 1), 6) > 4
    assert not consumption_count(four, month(2026, 1), 6) > 4

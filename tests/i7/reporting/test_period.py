"""Calendar-quarter resolution -- pure function, no DB needed."""

from datetime import date

import pytest

from app.initiatives.i7.reporting.period import resolve_quarter


@pytest.mark.parametrize(
    "quarter,expected_start,expected_end",
    [
        ("Q1 2026", date(2026, 1, 1), date(2026, 3, 31)),
        ("Q2 2026", date(2026, 4, 1), date(2026, 6, 30)),
        ("Q3 2026", date(2026, 7, 1), date(2026, 9, 30)),
        ("Q4 2026", date(2026, 10, 1), date(2026, 12, 31)),
    ],
)
def test_resolve_quarter_returns_correct_start_and_end(quarter, expected_start, expected_end):
    start, end = resolve_quarter(quarter)
    assert start == expected_start
    assert end == expected_end


@pytest.mark.parametrize(
    "bad_quarter",
    ["q3 2026", "Q5 2026", "Q0 2026", "2026 Q3", "Q3-2026", "", "Q3 26"],
)
def test_resolve_quarter_rejects_malformed_strings(bad_quarter):
    with pytest.raises(ValueError):
        resolve_quarter(bad_quarter)


def test_resolve_quarter_period_end_is_inclusive_last_day():
    _, end = resolve_quarter("Q1 2026")
    assert end == date(2026, 3, 31)
    # A bare "< period_end" filter would silently exclude March 31 -- callers
    # must add one day themselves (see service.py's _exclusive_end).


# --- the months of a period and how much of it has happened -------------------------------------

from app.initiatives.i7.reporting.period import (  # noqa: E402
    exclusive_end_of,
    is_period_complete,
    month_label,
    month_state,
    months_of,
    reporting_cutoff,
)


def test_the_exclusive_end_is_the_first_day_of_the_next_quarter_including_across_a_year():
    assert exclusive_end_of(date(2026, 12, 31)) == date(2027, 1, 1)
    assert exclusive_end_of(date(2026, 9, 30)) == date(2026, 10, 1)
    assert exclusive_end_of(date(2028, 2, 29)) == date(2028, 3, 1)  # a leap year


@pytest.mark.parametrize(
    "quarter,expected",
    [
        ("Q1 2026", [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]),
        ("Q4 2026", [date(2026, 10, 1), date(2026, 11, 1), date(2026, 12, 1)]),
        ("Q1 2027", [date(2027, 1, 1), date(2027, 2, 1), date(2027, 3, 1)]),
    ],
)
def test_a_quarter_has_exactly_its_three_months(quarter, expected):
    assert months_of(*resolve_quarter(quarter)) == expected


def test_month_labels():
    assert month_label(date(2026, 10, 1)) == "Oct 2026"
    assert month_label(date(2027, 1, 1)) == "Jan 2027"


def test_a_period_is_complete_only_after_its_last_day():
    end = date(2026, 12, 31)
    assert not is_period_complete(end, date(2026, 12, 31))  # the last day is still part of it
    assert is_period_complete(end, date(2027, 1, 1))
    assert not is_period_complete(end, date(2026, 10, 21))


def test_the_cutoff_is_today_while_the_period_runs_and_its_end_after():
    end = date(2026, 12, 31)
    assert reporting_cutoff(end, date(2026, 10, 21)) == date(2026, 10, 21)
    assert reporting_cutoff(end, date(2027, 2, 3)) == end


def test_each_month_is_ended_in_progress_or_not_started():
    end = date(2026, 12, 31)
    today = date(2026, 11, 15)
    assert month_state(date(2026, 10, 1), end, today) == "ended"
    assert month_state(date(2026, 11, 1), end, today) == "in_progress"
    assert month_state(date(2026, 12, 1), end, today) == "not_started"
    assert month_state(date(2026, 12, 1), end, date(2026, 12, 1)) == "in_progress"
    assert month_state(date(2026, 12, 1), end, date(2027, 1, 1)) == "ended"

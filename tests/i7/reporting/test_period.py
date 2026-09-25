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

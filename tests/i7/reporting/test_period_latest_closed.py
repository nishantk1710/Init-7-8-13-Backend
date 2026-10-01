"""Latest-closed-quarter resolution -- pure function, no DB needed."""

from datetime import date

import pytest

from app.initiatives.i7.reporting.period import latest_closed_quarter


@pytest.mark.parametrize(
    "today,expected",
    [
        (date(2026, 10, 1), "Q3 2026"),  # Q4 2026 in progress -> last closed is Q3 2026
        (date(2027, 1, 1), "Q4 2026"),  # Q1 2027 in progress -> wraps back to Q4 of prior year
        (date(2027, 4, 1), "Q1 2027"),  # Q2 2027 in progress -> Q1 2027
        (date(2027, 7, 1), "Q2 2027"),  # Q3 2027 in progress -> Q2 2027
    ],
)
def test_latest_closed_quarter_matches_expected_mapping(today, expected):
    assert latest_closed_quarter(today) == expected


def test_latest_closed_quarter_any_day_within_a_quarter_gives_the_same_answer():
    # The 1st and the 15th of the same quarter must resolve identically --
    # the function only cares which quarter `today` falls in, not the exact day.
    assert latest_closed_quarter(date(2026, 11, 15)) == "Q3 2026"
    assert latest_closed_quarter(date(2026, 12, 31)) == "Q3 2026"


def test_latest_closed_quarter_defaults_to_date_today():
    # No assertion on the actual value (that would just restate date.today()) --
    # only that the default path runs without requiring an explicit argument.
    result = latest_closed_quarter()
    assert isinstance(result, str) and result.startswith("Q")

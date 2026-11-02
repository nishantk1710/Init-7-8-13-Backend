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


def test_on_27_october_2026_the_latest_closed_quarter_is_q3_not_the_running_q4():
    # Today, at the time the automatic-generation work was scoped: Q4 2026 is only a month old.
    assert latest_closed_quarter(date(2026, 10, 27)) == "Q3 2026"


def test_the_last_day_of_q4_still_resolves_q3_and_the_first_day_of_the_next_year_resolves_q4():
    # The boundary is the day after quarter-end, not the quarter-end day itself: on 31 Dec Q4 has not closed yet.
    assert latest_closed_quarter(date(2026, 12, 31)) == "Q3 2026"
    assert latest_closed_quarter(date(2027, 1, 1)) == "Q4 2026"


@pytest.mark.parametrize(
    "today,quarter",
    [(date(2026, 10, 27), "Q3 2026"), (date(2027, 1, 1), "Q4 2026"), (date(2027, 4, 1), "Q1 2027")],
)
def test_the_resolved_quarter_is_always_complete_as_of_the_same_day(today, quarter):
    """The resolution and the completeness rule agree: whatever is "latest closed" is a finished period, so a report
    generated for it is never an in-progress one."""
    from app.initiatives.i7.reporting.period import is_period_complete, resolve_quarter

    assert latest_closed_quarter(today) == quarter
    assert is_period_complete(resolve_quarter(quarter)[1], today)


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

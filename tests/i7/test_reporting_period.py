"""Pure quarter/period resolution -- no database needed."""

from datetime import date

import pytest

from app.initiatives.i7.reporting.period import resolve_quarter


def test_q1_resolves_to_jan_1_through_mar_31():
    assert resolve_quarter("Q1 2026") == (date(2026, 1, 1), date(2026, 3, 31))


def test_q2_resolves_to_apr_1_through_jun_30():
    assert resolve_quarter("Q2 2026") == (date(2026, 4, 1), date(2026, 6, 30))


def test_q3_resolves_to_jul_1_through_sep_30():
    assert resolve_quarter("Q3 2026") == (date(2026, 7, 1), date(2026, 9, 30))


def test_q4_resolves_to_oct_1_through_dec_31():
    assert resolve_quarter("Q4 2026") == (date(2026, 10, 1), date(2026, 12, 31))


@pytest.mark.parametrize(
    "bad",
    [
        "q3 2026",  # lowercase
        "Q32026",  # no space
        "Q5 2026",  # out of range
        "Q0 2026",  # out of range
        "Quarter 3 2026",
        "2026 Q3",
        "",
        "Q3 26",  # 2-digit year
    ],
)
def test_malformed_quarter_strings_raise_value_error(bad):
    with pytest.raises(ValueError):
        resolve_quarter(bad)


def test_error_message_names_the_expected_format():
    with pytest.raises(ValueError, match=r"Q<1-4> <year>"):
        resolve_quarter("garbage")

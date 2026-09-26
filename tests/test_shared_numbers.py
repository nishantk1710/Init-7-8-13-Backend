"""Formatting a number for a sentence.

Small, and worth its own file because three modules had each written this and
all three had the same bug: they stripped trailing zeros but never rounded, so a
computed ratio reached the screen as
``5.29498153846153846153846154 months of cover``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.shared.numbers import plain


class TestRounding:
    def test_a_long_ratio_is_rounded_to_something_readable(self) -> None:
        """The bug this exists for."""
        assert plain(Decimal("5.29498153846153846153846154")) == "5.29"

    def test_it_rounds_half_up(self) -> None:
        assert plain(Decimal("2.005")) == "2.01"

    def test_a_large_rate_keeps_two_places(self) -> None:
        assert plain(Decimal("108333.3333333333333333")) == "108333.33"


class TestTidyness:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("3", "3"),
            ("3.000", "3"),
            ("2.50", "2.5"),
            ("2.5", "2.5"),
            ("0", "0"),
        ],
    )
    def test_trailing_zeros_are_dropped(self, value: str, expected: str) -> None:
        assert plain(Decimal(value)) == expected

    def test_a_round_number_never_becomes_scientific_notation(self) -> None:
        """Decimal.normalize() turns 30 into 3E+1, which is not a thing to put
        in a sentence."""
        assert plain(Decimal("30")) == "30"
        assert plain(Decimal("1000")) == "1000"

    def test_a_negative_number_survives(self) -> None:
        assert plain(Decimal("-2.5")) == "-2.5"


class TestAbsence:
    def test_none_is_unknown_not_zero(self) -> None:
        """Load-bearing across the platform: "no source told us" and "the answer
        is zero" are different facts, and only one should change somebody's mind
        about buying a part."""
        assert plain(None) == "unknown"

    def test_the_absent_word_can_be_chosen_by_the_caller(self) -> None:
        assert plain(None, unknown="not recorded") == "not recorded"

    def test_zero_is_still_zero(self) -> None:
        assert plain(Decimal("0")) == "0"

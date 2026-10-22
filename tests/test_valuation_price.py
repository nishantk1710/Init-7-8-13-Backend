"""The unit-price row rule (``app.shared.valuation_price``): pure, so every case is here.

The cases that matter, in the order the rule decides them:

* a plant's price comes from that plant's rows only -- the sodium-cyanide defect, where the
  highest price across plants (7,689.26, a per-tonne figure on two dormant plants) was used at
  plants valued at 14.58 and 15.45;
* split valuation: NEW, then NEW-INS; the blank aggregate row never, refurbished / damaged /
  scrap never; NEW wins by precedence and not by price;
* a placeholder (<= 1.00) is not a price, and does not hand the decision to another row;
* no price is a named reason, never a zero.
"""

from decimal import Decimal

import pytest

from app.shared.valuation_price import (
    DEFAULT_TOKEN_MAX,
    PriceMissing,
    Valuation,
    choose_unit_price,
    group_by_plant,
    normalise_valuation_type,
)


def v(price: str | None, bwtar: str | None = None, **kwargs) -> Valuation:
    return Valuation(bwtar, Decimal(price) if price is not None else None, **kwargs)


class TestNoRows:
    def test_no_row_for_the_plant_is_not_in_mbew(self) -> None:
        choice = choose_unit_price([])

        assert choice.price is None
        assert choice.missing is PriceMissing.NOT_IN_MBEW


class TestNotSplitValuated:
    def test_the_blank_row_is_the_price(self) -> None:
        choice = choose_unit_price([v("15.45")])

        assert choice.price == Decimal("15.45")
        assert choice.valuation_type is None
        assert choice.missing is None

    def test_currency_travels_with_the_price(self) -> None:
        choice = choose_unit_price([v("15.45", currency="ZAR")])

        assert (choice.price, choice.currency) == (Decimal("15.45"), "ZAR")

    def test_no_currency_stated_stays_none(self) -> None:
        assert choose_unit_price([v("15.45")]).currency is None


class TestSplitValuation:
    def test_new_is_the_price_and_the_blank_aggregate_row_is_ignored(self) -> None:
        """Material 43253306 as I08 measured it: NEW 38,122 against 34,840.62 on both the
        blank row and REFURBISHD. The blank row would have been the wrong price."""
        rows = [v("34840.62"), v("38122.00", "NEW"), v("34840.62", "REFURBISHD")]

        choice = choose_unit_price(rows)

        assert choice.price == Decimal("38122.00")
        assert choice.valuation_type == "NEW"

    def test_new_ins_is_used_when_there_is_no_new_row(self) -> None:
        choice = choose_unit_price([v("500.00", "NEW-INS"), v("90.00", "REFURBISHD"), v("480.00")])

        assert choice.price == Decimal("500.00")
        assert choice.valuation_type == "NEW-INS"

    @pytest.mark.parametrize("new_ins_price", ["10.00", "99999.00"])
    def test_new_wins_over_new_ins_by_precedence_not_by_price(self, new_ins_price: str) -> None:
        """Never the highest: NEW wins whether NEW-INS is far below it or far above it, and the
        order the rows arrive in changes nothing."""
        new, new_ins = v("500.00", "NEW"), v(new_ins_price, "NEW-INS")

        for rows in ([new, new_ins], [new_ins, new]):
            choice = choose_unit_price(rows)
            assert choice.price == Decimal("500.00")
            assert choice.valuation_type == "NEW"

    @pytest.mark.parametrize("condition", ["REFURBISHD", "REFRBSHD-I", "DAMAGED", "SCRAP"])
    def test_a_condition_row_is_never_a_replacement_price(self, condition: str) -> None:
        choice = choose_unit_price([v("5000.00", condition)])

        assert choice.price is None
        assert choice.missing is PriceMissing.NO_NEW_VALUATION
        assert condition in (choice.detail or "")

    def test_only_conditions_and_a_blank_row_is_no_new_valuation_not_the_blank_price(self) -> None:
        """The blank row of a split-valuated material is an aggregate; falling back to it would
        be the refurbished value in disguise."""
        choice = choose_unit_price([v("34840.62"), v("34840.62", "REFURBISHD"), v("100", "SCRAP")])

        assert choice.price is None
        assert choice.missing is PriceMissing.NO_NEW_VALUATION

    def test_a_new_row_with_no_usable_price_does_not_fall_through_to_new_ins(self) -> None:
        """The decision is made by the row existing, not by its price being usable -- falling
        through would be a fallback by another name."""
        choice = choose_unit_price([v("0.01", "NEW"), v("700.00", "NEW-INS")])

        assert choice.price is None
        assert choice.missing is PriceMissing.TOKEN_VALUE
        assert choice.valuation_type == "NEW"

    def test_valuation_types_are_compared_trimmed_and_upper_cased(self) -> None:
        assert normalise_valuation_type(" new-ins ") == "NEW-INS"
        assert normalise_valuation_type("") is None
        assert normalise_valuation_type("   ") is None
        assert normalise_valuation_type(None) is None


class TestPlaceholderPrices:
    @pytest.mark.parametrize("value", ["0.00", "0.01", "0.50", "1.00"])
    def test_at_or_below_one_is_a_placeholder(self, value: str) -> None:
        choice = choose_unit_price([v(value)])

        assert choice.price is None
        assert choice.missing is PriceMissing.TOKEN_VALUE
        assert choice.mbew_value == Decimal(value)

    def test_just_above_one_is_a_price(self) -> None:
        choice = choose_unit_price([v("1.01")])

        assert choice.price == Decimal("1.01")
        assert choice.missing is None

    def test_the_floor_is_the_default_one_rupee(self) -> None:
        assert DEFAULT_TOKEN_MAX == Decimal("1.00")

    def test_the_floor_is_configurable(self) -> None:
        assert choose_unit_price([v("5.00")], token_max=Decimal("10")).missing is PriceMissing.TOKEN_VALUE
        assert choose_unit_price([v("5.00")], token_max=Decimal("0.10")).price == Decimal("5.00")


class TestAmbiguousAndUnusableRows:
    def test_identical_duplicate_rows_are_not_ambiguous(self) -> None:
        choice = choose_unit_price([v("120.00", "NEW"), v("120.00", "NEW")])

        assert choice.price == Decimal("120.00")

    def test_duplicate_rows_that_disagree_have_no_winner_and_highest_is_not_chosen(self) -> None:
        choice = choose_unit_price([v("120.00", "NEW"), v("900.00", "NEW")])

        assert choice.price is None
        assert choice.missing is PriceMissing.AMBIGUOUS_PRICE
        assert "120.00" in (choice.detail or "") and "900.00" in (choice.detail or "")

    def test_the_same_price_in_two_currencies_is_ambiguous(self) -> None:
        choice = choose_unit_price([v("120.00", currency="ZAR"), v("120.00", currency="NAD")])

        assert choice.missing is PriceMissing.AMBIGUOUS_PRICE

    def test_an_unreadable_price_is_named(self) -> None:
        choice = choose_unit_price([v(None, "NEW", problem=PriceMissing.UNREADABLE_PRICE)])

        assert choice.missing is PriceMissing.UNREADABLE_PRICE

    def test_a_row_with_no_price_and_no_stated_problem_is_unreadable(self) -> None:
        assert choose_unit_price([v(None)]).missing is PriceMissing.UNREADABLE_PRICE

    @pytest.mark.parametrize("problem", [PriceMissing.BAD_PRICE_UNIT, PriceMissing.MISSING_CURRENCY])
    def test_the_rows_own_problem_is_the_reason(self, problem: PriceMissing) -> None:
        assert choose_unit_price([v(None, problem=problem)]).missing is problem

    def test_a_problem_row_is_never_read_as_a_price(self) -> None:
        """A row flagged bad is excluded even if a price value was left on it."""
        bad = Valuation(None, Decimal("500"), problem=PriceMissing.BAD_PRICE_UNIT)

        assert choose_unit_price([bad]).price is None

    def test_a_good_row_beside_a_bad_one_of_the_same_type_is_used(self) -> None:
        choice = choose_unit_price([v(None, "NEW", problem=PriceMissing.UNREADABLE_PRICE), v("80.00", "NEW")])

        assert choice.price == Decimal("80.00")


class TestThePerPlantRule:
    """The sodium-cyanide defect (material 1000000053), from the MBEW rows in the live extract."""

    ROWS = [
        ("1000000053", "1500", v("15.45")),
        ("1000000053", "1300", v("14.58")),
        ("1000000053", "1200", v("7689.26")),  # dormant plant: a per-tonne figure in a per-KG field
        ("1000000053", "1100", v("7689.26")),
    ]

    def test_each_plant_reads_its_own_price(self) -> None:
        by_plant = group_by_plant(self.ROWS)

        assert choose_unit_price(by_plant[("1000000053", "1500")]).price == Decimal("15.45")
        assert choose_unit_price(by_plant[("1000000053", "1300")]).price == Decimal("14.58")

    def test_the_old_highest_across_plants_answer_is_gone(self) -> None:
        """The old rule gave 7,689.26 at every plant -- ~500x too high at 1500 and 1300."""
        by_plant = group_by_plant(self.ROWS)

        for plant, own in (("1500", "15.45"), ("1300", "14.58")):
            price = choose_unit_price(by_plant[("1000000053", plant)]).price
            assert price == Decimal(own)
            assert price * 100 < Decimal("7689.26")

    def test_a_plant_with_no_row_of_its_own_borrows_nothing(self) -> None:
        by_plant = group_by_plant(self.ROWS)

        choice = choose_unit_price(by_plant.get(("1000000053", "3000"), []))

        assert choice.price is None
        assert choice.missing is PriceMissing.NOT_IN_MBEW

    def test_group_by_plant_keeps_every_row_of_a_plant_together(self) -> None:
        grouped = group_by_plant([("m", "1300", v("1.50")), ("m", "1300", v("2.50", "NEW")), ("m", "1500", v("3.50"))])

        assert len(grouped[("m", "1300")]) == 2
        assert len(grouped[("m", "1500")]) == 1

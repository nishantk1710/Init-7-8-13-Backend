"""New Item Price -- which MBEW row is a part's price new, and when there is none.

The rule is pure, so every case runs everywhere. The lookup is exercised on an
in-memory SQLite stand-in for ``v_mbew``: it reads only that view, through the
same chunked IN query it runs on SQL Server (where it reads the staged copy
instead -- ``valuation_sources`` only stages on SQL Server).

The cases that earn their place are the ones that would show a WRONG price
rather than none: the aggregate row of a split-valuated material read as its
NEW price, a placeholder R1 read as a price, and a price quoted per 10 units
read as per one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.initiatives.i8.config import I8Settings
from app.initiatives.i8.new_item_price import (
    PRICED,
    MissingPrice,
    NewItemPrice,
    Valuation,
    choose_price,
    fetch_new_item_prices,
    unit_price,
)

CFG = I8Settings()


def row(price: str | None, valuation_type: str | None = None) -> Valuation:
    return Valuation(valuation_type, Decimal(price) if price is not None else None)


class TestWhichRowIsThePrice:
    def test_a_material_that_is_not_split_reads_its_one_row(self) -> None:
        price = choose_price([row("1222.00")], "1300", CFG)

        assert price == NewItemPrice("1300", Decimal("1222.00"), mbew_value=Decimal("1222.00"))
        assert price.state == PRICED

    def test_a_split_material_reads_new_never_the_aggregate(self) -> None:
        """43253306 @1500 on 15-Oct-2026: the blank row equals REFURBISHD, so
        reading it would show a refurbished value as the price new."""
        rows = [row("34840.62"), row("38122.00", "NEW"), row("34840.62", "REFURBISHD")]

        price = choose_price(rows, "1500", CFG)

        assert price.price == Decimal("38122.00")
        assert price.valuation_type == "NEW"

    def test_a_split_material_without_a_new_row_has_no_price(self) -> None:
        """Not the blank row: on a split material it aggregates the other
        conditions, which is not what the part costs new."""
        price = choose_price([row("950.00"), row("950.00", "REFURBISHD")], "1500", CFG)

        assert price.price is None
        assert price.missing == MissingPrice.NO_NEW_VALUATION.value

    def test_no_row_at_the_plant_is_not_in_mbew(self) -> None:
        price = choose_price([], "1500", CFG)

        assert price == NewItemPrice("1500", None, missing=MissingPrice.NOT_IN_MBEW.value)

    def test_the_valuation_type_is_configurable(self) -> None:
        cfg = I8Settings(new_item_valuation_type="neu")
        rows = [row("10.00", "NEW"), row("20.00", "NEU")]

        assert choose_price(rows, "1300", cfg).price == Decimal("20.00")


class TestPlaceholderValues:
    @pytest.mark.parametrize("value", ["1.00", "0.01", "0.00", "-5.00"])
    def test_at_or_below_the_threshold_is_a_token_not_a_price(self, value: str) -> None:
        price = choose_price([row(value)], "1300", CFG)

        assert price.price is None
        assert price.missing == MissingPrice.TOKEN_VALUE.value
        # Kept, so the screen can say what MBEW holds instead of a price.
        assert price.mbew_value == Decimal(value)

    def test_just_above_the_threshold_is_a_price(self) -> None:
        assert choose_price([row("1.01")], "1300", CFG).price == Decimal("1.01")

    def test_a_token_on_the_new_row_keeps_its_valuation_type(self) -> None:
        price = choose_price([row("5000.00"), row("1.00", "NEW")], "1300", CFG)

        assert price.missing == MissingPrice.TOKEN_VALUE.value
        assert price.valuation_type == "NEW"

    def test_the_threshold_is_configurable(self) -> None:
        cfg = I8Settings(new_item_price_token_max=Decimal("5"))

        assert choose_price([row("4.99")], "1300", cfg).missing == MissingPrice.TOKEN_VALUE.value


class TestUnreadableValues:
    def test_an_unreadable_price_counts_as_no_row(self) -> None:
        assert choose_price([row(None)], "1300", CFG).missing == MissingPrice.NOT_IN_MBEW.value

    def test_an_unreadable_new_row_on_a_split_material_is_no_new_valuation(self) -> None:
        price = choose_price([row("900.00"), row(None, "NEW")], "1300", CFG)

        assert price.missing == MissingPrice.NO_NEW_VALUATION.value

    def test_duplicates_of_the_row_read_resolve_to_the_highest(self) -> None:
        assert choose_price([row("10.00"), row("12.00")], "1300", CFG).price == Decimal("12.00")


class TestUnitPrice:
    def test_verpr_is_per_peinh_units(self) -> None:
        assert unit_price(Decimal("250.00"), Decimal("10")) == Decimal("25")

    @pytest.mark.parametrize("peinh", [None, Decimal("0"), Decimal("-1"), "x"])
    def test_a_blank_or_non_positive_peinh_is_refused_not_assumed_one(self, peinh) -> None:
        assert unit_price(Decimal("250.00"), peinh) is None

    def test_text_and_floats_from_a_driver_are_read(self) -> None:
        assert unit_price("322.16", 1.0) == Decimal("322.16")

    def test_an_unreadable_verpr_is_none(self) -> None:
        assert unit_price(None, Decimal("1")) is None
        assert unit_price("n/a", Decimal("1")) is None


# --- The lookup, over a stand-in v_mbew --------------------------------------


@pytest.fixture
def db():
    engine = create_engine("sqlite://", future=True)
    with engine.begin() as connection:
        connection.execute(
            text("create table v_mbew (matnr text, bwkey text, bwtar text, verpr text, peinh text)")
        )
        connection.execute(
            text("insert into v_mbew values (:m, :p, :t, :v, :u)"),
            [
                # Not split, both plants: one price each.
                {"m": "5000024401", "p": "1300", "t": None, "v": "322.16", "u": "1"},
                {"m": "5000024401", "p": "1500", "t": None, "v": "309.26", "u": "1"},
                # Split: NEW is the price, the blank row is not.
                {"m": "43253306", "p": "1500", "t": None, "v": "34840.62", "u": "1"},
                {"m": "43253306", "p": "1500", "t": "NEW", "v": "38122.00", "u": "1"},
                {"m": "43253306", "p": "1500", "t": "REFURBISHD", "v": "34840.62", "u": "1"},
                # A placeholder.
                {"m": "43253210", "p": "1500", "t": None, "v": "1.00", "u": "1"},
                # Priced per 10.
                {"m": "4000002023", "p": "1300", "t": None, "v": "12220.00", "u": "10"},
                # Another material nobody asked about.
                {"m": "9999999999", "p": "1300", "t": None, "v": "5.00", "u": "1"},
            ],
        )
    with Session(engine) as session:
        yield session


class TestTheLookup:
    def test_one_price_per_plant_the_material_was_found_at(self, db) -> None:
        prices = fetch_new_item_prices(
            db,
            {
                "5000024401": {"1500", "1300"},
                "43253306": {"1500"},
                "43253210": {"1500"},
                "4000002023": {"1300"},
            },
            CFG,
        )

        assert [(p.plant, p.price) for p in prices["5000024401"]] == [
            ("1300", Decimal("322.16")),
            ("1500", Decimal("309.26")),
        ]
        assert prices["43253306"][0].price == Decimal("38122.00")
        assert prices["43253210"][0].missing == MissingPrice.TOKEN_VALUE.value
        assert prices["4000002023"][0].price == Decimal("1222")
        assert "9999999999" not in prices

    def test_a_plant_with_no_row_is_not_in_mbew(self, db) -> None:
        """43253306R @1500 on 15-Oct-2026: on a PO, absent from MBEW."""
        prices = fetch_new_item_prices(db, {"43253306R": {"1500"}, "5000024401": {"3000"}}, CFG)

        assert prices["43253306R"][0].missing == MissingPrice.NOT_IN_MBEW.value
        assert prices["5000024401"][0].missing == MissingPrice.NOT_IN_MBEW.value

    def test_a_material_with_no_plant_gets_no_entry(self, db) -> None:
        assert fetch_new_item_prices(db, {"5000024401": {None, ""}}, CFG) == {}

    def test_nothing_asked_reads_nothing(self) -> None:
        """No session at all: an empty request must not touch the database."""
        assert fetch_new_item_prices(None, {}, CFG) == {}


class TestAFailedLookup:
    """16-19 Oct 2026: the MBEW read ran for 12-16 minutes and took the whole
    coding screen down with it. A read that fails -- on SQL Server, one that
    runs past I8_NEW_ITEM_PRICE_TIMEOUT_SECONDS -- must cost the column, not
    the screen."""

    def test_every_plant_reads_lookup_failed_and_nothing_is_raised(self) -> None:
        # No v_mbew at all: the read fails the way a cancelled one does, as a
        # DBAPIError out of the driver.
        with Session(create_engine("sqlite://", future=True)) as db:
            prices = fetch_new_item_prices(db, {"5000024401": {"1500", "1300"}, "43253306": {"1500"}}, CFG)

        assert {material: [(p.plant, p.price, p.missing) for p in found] for material, found in prices.items()} == {
            "5000024401": [("1300", None, "LOOKUP_FAILED"), ("1500", None, "LOOKUP_FAILED")],
            "43253306": [("1500", None, "LOOKUP_FAILED")],
        }

    def test_it_is_not_reported_as_a_missing_valuation(self) -> None:
        """NOT_IN_MBEW is a fact about the material; a failed read is not."""
        with Session(create_engine("sqlite://", future=True)) as db:
            [price] = fetch_new_item_prices(db, {"5000024401": {"1300"}}, CFG)["5000024401"]

        assert price.state == MissingPrice.LOOKUP_FAILED.value != MissingPrice.NOT_IN_MBEW.value

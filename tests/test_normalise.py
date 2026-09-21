"""The normalise layer.

These are small functions guarding large mistakes. Each test names the failure
it prevents, because "pad(x) == y" on its own does not explain why anyone
should care.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.normalise import coerce, matnr


# --- Material numbers -------------------------------------------------------


def test_a_numeric_material_is_padded_to_eighteen() -> None:
    assert matnr.pad("2000000270") == "000000002000000270"


def test_an_alphanumeric_material_is_left_alone() -> None:
    """SAP's ALPHA exit pads only numeric values.

    Padding these would corrupt them into something that matches nothing --
    the same silent empty join this module exists to prevent.
    """
    assert matnr.pad("SPARE-12") == "SPARE-12"
    assert matnr.pad("ABC123") == "ABC123"


def test_padding_is_idempotent() -> None:
    """Applied twice by two code paths, it must not double-pad."""
    once = matnr.pad("2000000270")

    assert matnr.pad(once) == once


def test_blank_and_null_are_not_materials() -> None:
    assert matnr.pad(None) is None
    assert matnr.pad("   ") is None


def test_surrounding_whitespace_is_removed_before_padding() -> None:
    """SAP pads fixed-width fields; the spaces are formatting, not content."""
    assert matnr.pad("  2000000270  ") == "000000002000000270"


def test_an_overlong_value_is_not_truncated() -> None:
    """Longer than the declared width means the source is wrong about
    something. Trimming it here would hide that."""
    long_value = "9" * 20

    assert matnr.pad(long_value) == long_value


def test_stripping_gives_the_form_people_type() -> None:
    assert matnr.strip_padding("000000002000000270") == "2000000270"


def test_a_material_of_all_zeros_is_zero_not_nothing() -> None:
    assert matnr.strip_padding("000000000000000000") == "0"


def test_two_forms_of_the_same_material_compare_equal() -> None:
    """The join bug, stated as a question anyone can ask."""
    assert matnr.same_material("2000000270", "000000002000000270")
    assert not matnr.same_material("2000000270", "2000000271")


# --- Numbers ----------------------------------------------------------------


def test_the_space_padded_decimal_sap_actually_sends() -> None:
    """MARC safety stock arrives right-aligned in a 20-character field."""
    assert coerce.number("              0.000") == Decimal("0.000")
    assert coerce.number("             25.000") == Decimal("25.000")


def test_numbers_are_decimal_not_float() -> None:
    """These are quantities and money; a float cannot hold 0.1 exactly."""
    assert coerce.number("0.1") == Decimal("0.1")
    assert isinstance(coerce.number("0.1"), Decimal)


def test_an_unparseable_number_is_none_not_an_exception() -> None:
    """One bad cell must not cost the load of two million rows."""
    assert coerce.number("not a number") is None
    assert coerce.number("") is None
    assert coerce.number(None) is None


def test_integers_reject_a_real_fraction_but_accept_a_zero_one() -> None:
    assert coerce.integer("  7.000 ") == 7
    assert coerce.integer("7.5") is None


# --- Dates ------------------------------------------------------------------


def test_abap_dats() -> None:
    assert coerce.day("20260916") == date(2026, 9, 16)


def test_saps_no_date_is_none_not_year_zero() -> None:
    assert coerce.day("00000000") is None


def test_odata_epoch_wrapper() -> None:
    """A value that reached the raw layer as text lost its declared type."""
    assert coerce.day("/Date(1758067200000)/") == date(2025, 9, 17)


def test_iso_date_and_timestamp() -> None:
    assert coerce.day("2026-09-16") == date(2026, 9, 16)
    assert coerce.day("2026-09-16T13:14:03") == date(2026, 9, 16)


def test_an_already_decoded_datetime_passes_through() -> None:
    """The envelope decodes Edm.DateTime, so both forms reach this."""
    assert coerce.day(datetime(2026, 9, 16, 13, 14)) == date(2026, 9, 16)


def test_an_unparseable_date_is_none() -> None:
    assert coerce.day("not a date") is None
    assert coerce.day("") is None


# --- Text and flags ---------------------------------------------------------


def test_text_trims_saps_fixed_width_padding() -> None:
    assert coerce.text("ERSA      ") == "ERSA"
    assert coerce.text("      ") is None


def test_an_abap_checkbox() -> None:
    assert coerce.flag("X") is True
    assert coerce.flag(" ") is False
    assert coerce.flag(None) is False

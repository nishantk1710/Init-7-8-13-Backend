"""Raw value parsing by declared source format.

The SAP_CSV cases are values read from the live Azure SQL tables on
2026-09-27 (raw_mseg, raw_ekbe, raw_mard, raw_marc, raw_ekpo) -- not
invented shapes.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.core.raw_values import SAP_CSV, WORKBOOK, parse_date, parse_decimal


# --- The reason this module exists ------------------------------------------


def test_the_same_text_is_a_different_number_per_format():
    """No rule on the value can decide this -- only the declared format."""
    assert parse_decimal("1,000", WORKBOOK) == Decimal("1000")
    assert parse_decimal("1,000", SAP_CSV) == Decimal("1")


# --- SAP CSV (German user settings) -----------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0,989", Decimal("0.989")),  # raw_mseg.MENGE
        ("1,000", Decimal("1.000")),  # raw_ekbe.MENGE
        ("0,000", Decimal("0")),  # raw_mard.LABST
        ("0,00", Decimal("0")),  # raw_marc.VKUMC
        ("0", Decimal("0")),  # raw_ekpo.PLIFZ
        ("1.234,500", Decimal("1234.500")),
        ("12.345.678,9", Decimal("12345678.9")),
        ("5,000-", Decimal("-5.000")),  # SAP's trailing minus
        ("-5,000", Decimal("-5.000")),
        ("0,0000000000000000E+00", Decimal("0")),  # raw_marc.FDICH
        ("  2,5  ", Decimal("2.5")),
    ],
)
def test_sap_csv_numbers(raw, expected):
    assert parse_decimal(raw, SAP_CSV) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "0.989",  # a dot-decimal value in a comma-decimal source: format changed
        "1.5",
        "1,234.5",  # workbook shape
        "12.34,5",  # bad grouping
        "-5,000-",  # two signs
        "abc",
        "NaN",
        "Infinity",
        "1_000",
    ],
)
def test_sap_csv_rejects_what_does_not_fit_rather_than_guessing(raw):
    assert parse_decimal(raw, SAP_CSV) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("07.03.2019", date(2019, 3, 7)),  # raw_ekbe.BUDAT
        ("28.05.2018", date(2018, 5, 28)),  # raw_cdhdr.UDATE
        ("00.00.0000", None),  # raw_ekpo.AEDAT: SAP's "no date"
        ("00000000", None),
        ("", None),
        (None, None),
        ("2019-03-07", None),  # ISO in a German-format source: format changed
        ("31.02.2019", None),
    ],
)
def test_sap_csv_dates(raw, expected):
    assert parse_date(raw, SAP_CSV) == expected


# --- Workbook (July extract) -- unchanged behaviour --------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("4", Decimal("4")),
        ("2766.096", Decimal("2766.096")),
        ("1,234.5", Decimal("1234.5")),
        ("1,234,567", Decimal("1234567")),
        ("-3", Decimal("-3")),
        ("1e-05", Decimal("0.00001")),
        ("", None),
        ("abc", None),
    ],
)
def test_workbook_numbers(raw, expected):
    assert parse_decimal(raw, WORKBOOK) == expected


@pytest.mark.parametrize("raw", ["12,34", "0,989", "5,000-", "NaN"])
def test_workbook_rejects_malformed(raw):
    assert parse_decimal(raw, WORKBOOK) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-03-17", date(2026, 3, 17)),
        ("00000000", None),
        ("0000-00-00", None),
        ("17.03.2026", None),
        ("not-a-date", None),
    ],
)
def test_workbook_dates(raw, expected):
    assert parse_date(raw, WORKBOOK) == expected


# --- Typed values bypass the text format -------------------------------------


@pytest.mark.parametrize("fmt", [WORKBOOK, SAP_CSV])
def test_database_typed_values_are_not_reparsed(fmt):
    """SUM(...) arrives as Decimal("12.5"); reading its str() as German would
    turn the database's own decimal point into a thousands separator."""
    assert parse_decimal(Decimal("12.5"), fmt) == Decimal("12.5")
    assert parse_decimal(3, fmt) == Decimal(3)
    assert parse_decimal(0.25, fmt) == Decimal("0.25")
    assert parse_decimal(Decimal("NaN"), fmt) is None
    assert parse_decimal(True, fmt) is None
    assert parse_date(date(2026, 1, 2), fmt) == date(2026, 1, 2)
    assert parse_date(datetime(2026, 1, 2, 22, 0), fmt) == date(2026, 1, 2)

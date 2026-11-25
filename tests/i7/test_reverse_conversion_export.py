"""The Min-Max -> OAR CSV export: encoding, self-description, and formula-injection protection."""

import csv
import io
from datetime import date
from decimal import Decimal

import pytest
from reverse_conversion_support import ANCHOR, FakeSource, attr, population, sparse

from app.initiatives.i7.policy.reverse_conversion import current_reverse_conversion_policy
from app.initiatives.i7.reverse_conversion.export import BOM, COLUMNS, _number, csv_safe, rows_to_csv
from app.initiatives.i7.reverse_conversion.metrics import resolve_anchor
from app.initiatives.i7.reverse_conversion.service import build_report
from app.initiatives.i7.reverse_conversion.types import ReverseCategory as Cat
from reverse_conversion_support import month

POLICY = current_reverse_conversion_policy()


def make_report(source):
    return build_report(source, POLICY, resolve_anchor(latest_staged_month=date(2026, 11, 1), data_loaded_at=None, configured_anchor=ANCHOR))


def parse(content: bytes) -> list[dict[str, str]]:
    text = content.decode("utf-8")
    assert text.startswith(BOM)
    return list(csv.DictReader(io.StringIO(text[len(BOM):], newline="")))


def test_the_file_is_utf_8_with_a_byte_order_mark_and_crlf_line_ends():
    report = make_report(FakeSource(*population()))
    content = rows_to_csv(report.rows, report.anchor, report.policy)
    assert content.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in content and b"\n" not in content.replace(b"\r\n", b"")


def test_the_header_is_the_documented_column_list_and_every_row_has_every_column():
    report = make_report(FakeSource(*population()))
    rows = parse(rows_to_csv(report.rows, report.anchor, report.policy))
    assert tuple(rows[0].keys()) == COLUMNS
    assert len(rows) == len(report.rows) == 18
    assert all(len(row) == len(COLUMNS) for row in rows)


def test_every_row_carries_the_anchor_and_the_policy_version():
    report = make_report(FakeSource(*population()))
    rows = parse(rows_to_csv(report.rows, report.anchor, report.policy))
    assert {row["anchor_month"] for row in rows} == {"2026-10"}
    assert {row["policy_version"] for row in rows} == {"reverse-conversion-v1"}


def test_category_reason_and_the_consumption_figures_are_exported():
    report = make_report(FakeSource(*population()))
    by = {row["material"]: row for row in parse(rows_to_csv(report.rows, report.anchor, report.policy))}
    rec = by["REC1"]
    assert rec["category"] == "Recommended for OAR" and rec["reason"] == "Sparse recurring demand"
    assert rec["issues_latest_12m"] == "2" and rec["months_with_consumption_12m"] == "2"
    assert rec["first_valid_movement_month"] == "2015-01" and rec["last_valid_movement_month"] == "2026-08"
    assert rec["months_since_last_movement"] == "2"
    assert by["INCONS"]["reason"] == "Inconsistent movement data"
    assert by["NEVER"]["first_valid_movement_month"] == "" and by["NEVER"]["history_span_months"] == ""


def test_stock_value_is_exported_and_the_currency_is_left_empty():
    report = make_report(FakeSource(*population()))
    by = {row["material"]: row for row in parse(rows_to_csv(report.rows, report.anchor, report.policy))}
    assert by["REC2"]["current_stock"] == "500" and by["REC2"]["unit_price"] == "10" and by["REC2"]["stock_value"] == "5000"
    assert by["REC3"]["stock_value"] == "" and by["REC3"]["unit_price"] == ""  # no price: no value, nothing invented
    assert by["REC4"]["current_stock"] == "0"  # zero stock stays visible
    assert {row["currency"] for row in by.values()} == {""}


def test_a_filtered_export_holds_only_the_rows_given():
    report = make_report(FakeSource(*population()))
    only = [row for row in report.rows if row.category is Cat.NOT_ELIGIBLE]
    rows = parse(rows_to_csv(only, report.anchor, report.policy))
    assert {row["category"] for row in rows} == {"Not Eligible"} and len(rows) == 3


def test_an_empty_export_is_just_the_header():
    report = make_report(FakeSource(*population()))
    rows = parse(rows_to_csv([], report.anchor, report.policy))
    assert rows == []
    assert rows_to_csv([], report.anchor, report.policy).decode("utf-8").lstrip(BOM).strip() == ",".join(COLUMNS)


# --- Formula injection --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["=1+1", "+1", "-1", "@SUM(A1)", "\tcmd", "\rcmd", '=HYPERLINK("http://x","y")'])
def test_a_text_that_a_spreadsheet_would_run_gets_a_leading_apostrophe(value):
    assert csv_safe(value) == "'" + value


@pytest.mark.parametrize("value", ["SULPHUR PRILLS", "1000123", "a=b", "x-1", " =1", "", "100%"])
def test_ordinary_text_is_left_alone(value):
    assert csv_safe(value) == value


def test_none_becomes_an_empty_cell():
    assert csv_safe(None) == ""


def test_a_dangerous_description_is_neutralised_in_the_file():
    source = FakeSource(
        [attr("A1", description='=HYPERLINK("http://evil","x")'), attr("A2", description="+cmd|' /C calc'!A0"), attr("A3", description="@SUM(1)"), attr("A4", description="-2+3"), attr("A5", description="HOSE")],
        {(m, "1300"): sparse(month(2026, 5)) for m in ("A1", "A2", "A3", "A4", "A5")},
    )
    report = make_report(source)
    by = {row["material"]: row for row in parse(rows_to_csv(report.rows, report.anchor, report.policy))}
    assert by["A1"]["description"].startswith("'=")
    assert by["A2"]["description"].startswith("'+")
    assert by["A3"]["description"].startswith("'@")
    assert by["A4"]["description"].startswith("'-")
    assert by["A5"]["description"] == "HOSE"


def test_a_dangerous_material_number_is_neutralised_too():
    report = make_report(FakeSource([attr("=CMD")], {("=CMD", "1300"): sparse(month(2026, 5))}))
    rows = parse(rows_to_csv(report.rows, report.anchor, report.policy))
    assert rows[0]["material"] == "'=CMD"


def test_the_explanation_column_is_protected_as_well_as_the_description():
    # Explanations are generated here, but they are text cells all the same.
    report = make_report(FakeSource(*population()))
    for row in parse(rows_to_csv(report.rows, report.anchor, report.policy)):
        assert not row["explanation"].startswith(("=", "+", "-", "@"))


def test_numbers_are_never_prefixed_even_when_they_could_be_negative():
    # Numeric columns go through _number, not csv_safe: a negative figure stays a plain number.
    assert _number(Decimal("-5.50")) == "-5.5"
    assert _number(-3) == "-3"
    assert csv_safe("-5.5") == "'-5.5"  # ...whereas the same characters as TEXT are protected


@pytest.mark.parametrize(
    ("value", "text"),
    [(Decimal("1500.000"), "1500"), (Decimal("12.5000"), "12.5"), (Decimal("0.000"), "0"), (Decimal("1E+3"), "1000"), (None, ""), (7, "7")],
)
def test_numbers_are_written_plainly(value, text):
    assert _number(value) == text

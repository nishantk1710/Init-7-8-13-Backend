"""The Recommendations Excel export's workbook (the endpoint itself needs the database, which is not
reachable locally, so the workbook is built and read back here)."""

from decimal import Decimal

from openpyxl import load_workbook

from app.api.i7.recommendations import router
from app.initiatives.i7.reporting.recommendations_export import COLUMNS, SHEET_NAME, build_recommendations_workbook


def _row(**over):
    base = {
        "Material Number": "000000000800000001",
        "Material Description": "Bearing, 6205",
        "Plant": "1300",
        "Recommendation Type": "OAR",
        "Criticality": "High",
        "Status": "READY_FOR_REVIEW",
        "Current ROP (SAP)": Decimal("10.500"),
        "Current Safety Stock (SAP)": None,
        "Current Max Stock (SAP)": Decimal("0"),
        "Recommended ROP": Decimal("12"),
        "Recommended Safety Stock": Decimal("3.250"),
        "Recommended Max Stock": Decimal("40"),
        "Counted in Working Capital": "Yes",
        "Reason": "Sized from similar materials.",
    }
    base.update(over)
    return [base[column] for column in COLUMNS]


def _sheet(rows):
    return load_workbook(build_recommendations_workbook(rows))[SHEET_NAME]


def test_header_row_columns_and_sheet_name():
    sheet = _sheet([_row()])
    assert [cell.value for cell in sheet[1]] == list(COLUMNS)
    for column in ("Material Description", "Current ROP (SAP)", "Recommended Max Stock", "Criticality", "Reason"):
        assert column in COLUMNS


def test_quantities_are_real_numbers_and_a_missing_one_is_empty_not_zero():
    sheet = _sheet([_row()])
    row = {sheet.cell(1, c).value: sheet.cell(2, c) for c in range(1, len(COLUMNS) + 1)}
    assert row["Current ROP (SAP)"].value == 10.5 and row["Current ROP (SAP)"].data_type == "n"
    assert row["Recommended Safety Stock"].value == 3.25
    assert row["Current Max Stock (SAP)"].value == 0  # a real zero stays a zero
    assert row["Current Safety Stock (SAP)"].value is None  # no value stays empty


def test_text_that_looks_like_a_formula_stays_text():
    sheet = _sheet([_row(**{"Material Description": "=SUM(A1:A9)", "Reason": "+cmd|' /C calc'!A0"})])
    description, reason = sheet.cell(2, 2), sheet.cell(2, len(COLUMNS))
    assert description.data_type == "s" and description.value == "=SUM(A1:A9)"
    assert reason.data_type == "s"


def test_control_characters_do_not_break_the_file():
    sheet = _sheet([_row(Reason="bad\x00char\x0b here")])
    assert sheet.cell(2, len(COLUMNS)).value == "badchar here"


def test_header_is_frozen_and_filterable_and_reason_wraps():
    sheet = _sheet([_row(), _row()])
    assert sheet.freeze_panes == "A2"
    assert sheet.auto_filter.ref == "A1:N3"
    assert sheet.cell(2, len(COLUMNS)).alignment.wrap_text is True
    assert sheet.cell(1, 1).font.bold is True


def test_counted_in_working_capital_says_yes_or_no_with_the_reason():
    from app.initiatives.i7.reporting.recommendations_export import counted_label

    assert counted_label(True) == "Yes"
    assert counted_label(False, "no price") == "No - no price"
    assert counted_label(False) == "No"
    sheet = _sheet([_row(), _row(**{"Counted in Working Capital": counted_label(False, "no price")})])
    column = COLUMNS.index("Counted in Working Capital") + 1
    assert [sheet.cell(2, column).value, sheet.cell(3, column).value] == ["Yes", "No - no price"]
    assert COLUMNS.index("Counted in Working Capital") < COLUMNS.index("Reason")  # the wide reason stays last


def test_no_rows_still_gives_a_valid_workbook_with_headers():
    sheet = _sheet([])
    assert [cell.value for cell in sheet[1]] == list(COLUMNS)
    assert sheet.max_row == 1


def test_export_route_is_registered_before_the_id_route():
    paths = [route.path for route in router.routes]
    assert paths.index("/recommendations/export") < paths.index("/recommendations/{recommendation_id}")

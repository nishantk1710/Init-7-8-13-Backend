"""Reading a plant's monthly criticality report: finding the data sheet,
refusing what cannot be trusted, and counting what can."""

from __future__ import annotations

from io import BytesIO

import pytest

from app.integrations.criticality.report_parser import (
    CriticalityReportError,
    RowCollector,
    material_key,
    parse_month,
    parse_report,
)
from tests.criticality.conftest import HEADER, workbook


def test_the_data_sheet_is_found_by_its_columns_not_its_name_or_position() -> None:
    parsed = parse_report(workbook([("8000004187", "NORMAL")], data_sheet="Aug extract"), plant="1300")
    assert parsed.sheet_name == "Aug extract"
    assert [r.material for r in parsed.rows] == ["8000004187"]


def test_both_delivered_shapes_are_read() -> None:
    """BMM: no Sl.No column, header on row 2. GB: Sl.No first, header on row 2."""
    bmm_header = ["Mat.Code", "Material Descrip", "Old Material", "Plant", "MatTyp", "Criticality", "MRP Type"]
    book = BytesIO()
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.title = "Sheet3"
    sheet = wb.create_sheet("Sheet1")
    sheet.append([])
    sheet.append(bmm_header)
    sheet.append([8000004187, "ROCK DRILL", "", "1300", "ZREP", "NORMAL", "PD"])
    wb.save(book)
    book.seek(0)
    parsed = parse_report(book, plant="1300")
    row = parsed.rows[0]
    assert (row.material, row.criticality, row.description, row.mattyp, row.mrp_type) == (
        "8000004187", "NORMAL", "ROCK DRILL", "ZREP", "PD",
    )
    assert parse_report(workbook([("5000068389", "CRITICAL")], plant="1500"), plant="1500").rows[0].criticality == "CRITICAL"


def test_a_header_on_the_first_row_is_found_too() -> None:
    assert len(parse_report(workbook([("1", "NORMAL")], title_row=False), plant="1300").rows) == 1


def test_numeric_material_cells_lose_their_float_suffix_and_padding() -> None:
    parsed = parse_report(workbook([(8000004187.0, "NORMAL"), ("000000008000004556", "IMPACT")]), plant="1300")
    assert [r.material for r in parsed.rows] == ["8000004187", "8000004556"]
    assert parsed.rows[1].material_raw == "000000008000004556"


def test_a_sheet_without_the_required_columns_is_refused_by_name() -> None:
    header = [h for h in HEADER if h != "Criticality"]
    with pytest.raises(CriticalityReportError, match="Mat.Code, Plant and Criticality"):
        parse_report(workbook([("1", "NORMAL")], header=header), plant="1300")


def test_a_file_that_is_not_a_workbook_is_refused() -> None:
    with pytest.raises(CriticalityReportError, match="Excel workbook"):
        parse_report(BytesIO(b"mat_code,plant\n1,1300\n"), plant="1300")


def test_a_file_for_another_plant_is_refused_and_says_which() -> None:
    with pytest.raises(CriticalityReportError, match=r"plant 1500 \(2 rows\), but you picked 1300"):
        parse_report(workbook([("1", "NORMAL", "1500"), ("2", "NORMAL", "1500")]), plant="1300")


def test_a_file_mixing_plants_is_refused() -> None:
    with pytest.raises(CriticalityReportError, match="as well as 1300"):
        parse_report(workbook([("1", "NORMAL", "1300"), ("2", "NORMAL", "1500")]), plant="1300")


def test_a_tier_that_is_not_one_of_the_five_is_refused_not_blanked() -> None:
    with pytest.raises(CriticalityReportError, match=r"Unrecognised: 'CRITCAL' \(1\)"):
        parse_report(workbook([("1", "CRITCAL"), ("2", "NORMAL")]), plant="1300")


def test_tiers_are_read_case_and_space_insensitively() -> None:
    parsed = parse_report(workbook([("1", " critical "), ("2", "Normal")]), plant="1300")
    assert [r.criticality for r in parsed.rows] == ["CRITICAL", "NORMAL"]


def test_blank_tiers_are_kept_and_counted() -> None:
    parsed = parse_report(workbook([("1", None), ("2", "NORMAL"), ("3", "  ")]), plant="1300")
    assert parsed.blank_tiers == 2
    assert [r.criticality for r in parsed.rows] == [None, "NORMAL", None]
    assert dict(parsed.tiers) == {"NORMAL": 1}


def test_a_repeated_material_with_the_same_tier_collapses_and_is_counted() -> None:
    parsed = parse_report(workbook([("1", "NORMAL"), ("0001", "NORMAL"), ("2", "IMPACT")]), plant="1300")
    assert (len(parsed.rows), parsed.rows_read, parsed.duplicate_rows) == (2, 3, 1)


def test_a_repeated_material_with_two_tiers_is_refused() -> None:
    with pytest.raises(CriticalityReportError, match=r"1 material\(s\) appear more than once.*1 \(CRITICAL / NORMAL\)"):
        parse_report(workbook([("1", "NORMAL"), ("1", "CRITICAL")]), plant="1300")


def test_a_tiered_repeat_beats_a_blank_first_occurrence() -> None:
    parsed = parse_report(workbook([("1", None), ("1", "IMPACT")]), plant="1300")
    assert [r.criticality for r in parsed.rows] == ["IMPACT"]


def test_footer_rows_without_a_material_are_skipped_and_counted() -> None:
    parsed = parse_report(workbook([("1", "NORMAL"), (None, None)]), plant="1300")
    assert (len(parsed.rows), parsed.skipped_blank_material) == (1, 1)


def test_a_sheet_with_no_data_rows_is_refused() -> None:
    with pytest.raises(CriticalityReportError, match="no material rows"):
        parse_report(workbook([]), plant="1300")


@pytest.mark.parametrize("raw, expected", [("2026-08", "2026-08-01"), ("2026-08-15", "2026-08-01")])
def test_the_report_month_is_the_first_of_the_month(raw, expected) -> None:
    assert parse_month(raw).isoformat() == expected


@pytest.mark.parametrize("raw", ["", "August", "2026-13"])
def test_a_report_month_that_is_not_a_month_is_refused(raw) -> None:
    with pytest.raises(CriticalityReportError):
        parse_month(raw)


def test_the_material_key_matches_the_normalise_views() -> None:
    assert material_key(" 000000008000000000 ") == "8000000000"
    assert material_key("SPARE-12") == "SPARE-12"  # alphanumeric: left as typed
    assert material_key("000") == "000"  # all zeros stays rather than becoming empty


def test_the_delivered_tables_are_read_leniently() -> None:
    """Nothing in a delivery can be refused: unknown tier -> blank, other
    plants dropped, first of two conflicting rows kept -- all reported."""
    c = RowCollector("1300", strict=False)
    c.add("1", "1300", "NORMAL")
    c.add("1", "1300", "CRITICAL")
    c.add("2", "1300", "WHATEVER")
    c.add("3", "1500", "NORMAL")
    parsed = c.finish("raw_zmm065_bmm")
    assert [(r.material, r.criticality) for r in parsed.rows] == [("1", "NORMAL"), ("2", None)]
    assert len(parsed.issues) >= 2

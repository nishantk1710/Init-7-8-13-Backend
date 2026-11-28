"""The Stock item to OAR tab's Excel export, and its "Counted in Working Capital" column.

The workbook itself needs no database. The flag is the Working Capital card's own rule, so it is tested against the
card's calculation (``working_capital_impact`` in Stock item to OAR mode) on SQLite, and the route is driven over a
calculated report with the database dependency pointed at the same SQLite.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from reverse_conversion_support import FakeSource, population
from sqlalchemy import select

from app.api.i7.reverse_conversion import get_reverse_report
from app.api.i7.reverse_conversion_excel import NO_RECOMMENDATION_NOTE, NOT_ELIGIBLE_NOTE, counted_labels
from app.core.db import get_db
from app.initiatives.i7.kpi_formulas.resolved import load_active
from app.initiatives.i7.kpi_formulas.working_capital import working_capital_impact
from app.initiatives.i7.policy.reverse_conversion import current_reverse_conversion_policy
from app.initiatives.i7.reverse_conversion.export import COLUMNS, COUNTED_COLUMN, EXCEL_COLUMNS, EXCEL_SHEET, rows_to_xlsx
from app.initiatives.i7.reverse_conversion.metrics import resolve_anchor
from app.initiatives.i7.reverse_conversion.service import build_report, clear_report_cache
from app.main import app
from app.models.i7_recommendation import Recommendation
from tests.i7.test_working_capital_stock_to_oar import db, request, run, stock, verdict  # noqa: F401  (db is a fixture)

URL = "/api/v1/i7/reverse-conversion/recommendations/export-excel"


@pytest.fixture
def report():
    clear_report_cache()
    source = FakeSource(*population(), data_loaded_at=datetime(2026, 11, 23, tzinfo=timezone.utc))
    anchor = resolve_anchor(latest_staged_month=date(2026, 11, 1), data_loaded_at=None, configured_anchor=date(2026, 10, 1))
    built = replace(
        build_report(source, current_reverse_conversion_policy(), anchor),
        generated_at=datetime(2026, 11, 26, 2, 30, tzinfo=timezone.utc),
        run_id=7,
    )
    yield built
    clear_report_cache()


def sheet_of(workbook: BytesIO):
    return load_workbook(workbook)[EXCEL_SHEET]


def as_dicts(sheet):
    header = [cell.value for cell in sheet[1]]
    return [dict(zip(header, [cell.value for cell in row])) for row in sheet.iter_rows(min_row=2)]


# --- the workbook ---------------------------------------------------------------------------------------------------


def test_the_columns_are_the_csv_ones_plus_the_counted_flag_right_after_the_oar_conversion(report):
    assert EXCEL_COLUMNS == COLUMNS[:7] + (COUNTED_COLUMN,) + COLUMNS[7:]
    sheet = sheet_of(rows_to_xlsx(report.rows, report.anchor, report.policy, {}))
    assert [cell.value for cell in sheet[1]] == list(EXCEL_COLUMNS)
    assert EXCEL_COLUMNS[EXCEL_COLUMNS.index("oar_conversion") + 1] == "Counted in Working Capital"


def test_each_row_reads_what_the_caller_said_and_no_when_it_said_nothing(report):
    labels = {("REC1", "1300"): "Yes", ("REC2", "1300"): "No - no price"}
    rows = as_dicts(sheet_of(rows_to_xlsx(report.rows, report.anchor, report.policy, labels)))
    by_material = {row["material"]: row[COUNTED_COLUMN] for row in rows}

    assert by_material["REC1"] == "Yes" and by_material["REC2"] == "No - no price"
    assert by_material["REC3"] == "No"
    assert len(rows) == len(report.rows)


def test_quantities_are_numbers_and_a_missing_value_is_an_empty_cell(report):
    rows = {r["material"]: r for r in as_dicts(sheet_of(rows_to_xlsx(report.rows, report.anchor, report.policy, {})))}

    assert rows["REC1"]["unit_price"] == 10 and rows["REC1"]["stock_value"] == 1000
    assert rows["REC3"]["unit_price"] is None  # no price: empty, never 0
    assert rows["REC1"]["anchor_month"] == "2026-10"


def test_text_that_looks_like_a_formula_stays_text(report):
    first = replace(report.rows[0], description="=SUM(A1:A9)")
    workbook = rows_to_xlsx([first], report.anchor, report.policy, {})
    cell = sheet_of(workbook).cell(2, EXCEL_COLUMNS.index("description") + 1)

    assert cell.data_type == "s" and cell.value == "=SUM(A1:A9)"  # no apostrophe: the cell is text


def test_the_header_is_frozen_and_filterable_and_an_empty_export_is_still_a_valid_workbook(report):
    sheet = sheet_of(rows_to_xlsx(report.rows, report.anchor, report.policy, {}))
    assert sheet.freeze_panes == "A2"
    assert sheet.auto_filter.ref.startswith("A1:") and sheet.auto_filter.ref.endswith(str(len(report.rows) + 1))
    assert sheet_of(rows_to_xlsx([], report.anchor, report.policy, {})).max_row == 1


# --- the flag is the card's own rule --------------------------------------------------------------------------------


def rows_for(report, *materials):
    return [row for row in report.rows if row.material in materials]


def test_the_flag_says_yes_or_the_reason_for_each_kind_of_row(report, db):  # noqa: F811
    stock(db, "REC1", price="100", current="5")  # priced in ZAR: counts
    stock(db, "REC2", price="1.00", current="5")  # a placeholder price
    stock(db, "REC3", price="100", current="5", currency="USD")  # a currency that cannot be stated in ZAR
    stock(db, "REC4", price="100", current="5")  # its conversion was rejected
    latest = run(db)
    for material in ("REC1", "REC2", "REC3", "REC4"):
        verdict(db, latest, material)
    request(db, "REC4", "REJECTED")

    labels = counted_labels(db, rows_for(report, "REC1", "REC2", "REC3", "REC4", "REC5", "PROT"))

    assert labels[("REC1", "1300")] == "Yes"
    assert labels[("REC2", "1300")].startswith("No - placeholder price")
    assert labels[("REC3", "1300")] == "No - price not in ZAR or NAD"
    assert labels[("REC4", "1300")] == "No - conversion request rejected"
    assert labels[("REC5", "1500")] == f"No - {NO_RECOMMENDATION_NOTE}"  # eligible, but no Stock item recommendation to value
    assert labels[("PROT", "1300")] == f"No - {NOT_ELIGIBLE_NOTE}"


def test_the_materials_flagged_yes_are_exactly_the_ones_the_card_counts(report, db):  # noqa: F811
    stock(db, "REC1", price="100", current="5")
    stock(db, "REC2", price="1.00", current="5")
    stock(db, "REC3", price="100", current="5", currency="USD")
    stock(db, "REC4", price="50", current="2")
    latest = run(db)
    for material in ("REC1", "REC2", "REC3", "REC4"):
        verdict(db, latest, material)

    labels = counted_labels(db, rows_for(report, "REC1", "REC2", "REC3", "REC4"))
    yes = {key[0] for key, label in labels.items() if label == "Yes"}
    card = working_capital_impact(db, select(Recommendation), load_active(db), stock_to_oar="replace")

    assert yes == {"REC1", "REC4"}
    assert card.included_count == len(yes) and card.amount == Decimal("500") + Decimal("100")


# --- the route ------------------------------------------------------------------------------------------------------


@pytest.fixture
def client(report, db):  # noqa: F811
    stock(db, "REC1", price="100", current="5")
    verdict(db, run(db), "REC1")
    app.dependency_overrides[get_reverse_report] = lambda: report
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.pop(get_reverse_report, None)
    app.dependency_overrides.pop(get_db, None)


def test_the_route_returns_an_xlsx_attachment_named_for_the_anchor(client):
    response = client.get(URL)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert response.headers["content-disposition"] == 'attachment; filename="i7-stock-item-to-oar-2026-10.xlsx"'
    rows = {r["material"]: r for r in as_dicts(sheet_of(BytesIO(response.content)))}
    assert rows["REC1"][COUNTED_COLUMN] == "Yes"
    assert rows["REC2"][COUNTED_COLUMN] == f"No - {NO_RECOMMENDATION_NOTE}"
    assert rows["PROT"][COUNTED_COLUMN] == f"No - {NOT_ELIGIBLE_NOTE}"


def test_the_route_follows_the_same_filters_as_the_list_and_refuses_a_bad_one(client, report):
    eligible = as_dicts(sheet_of(BytesIO(client.get(URL, params={"eligibility": "eligible"}).content)))

    assert eligible and all(row["oar_conversion"] == "Eligible" for row in eligible)
    assert {row["material"] for row in as_dicts(sheet_of(BytesIO(client.get(URL, params={"search": "rec1"}).content)))} == {"REC1"}
    assert client.get(URL, params={"plant": "9999"}).status_code == 400


def test_the_csv_export_is_still_there_unchanged(client):
    assert client.get("/api/v1/i7/reverse-conversion/recommendations/export").headers["content-type"].startswith("text/csv")

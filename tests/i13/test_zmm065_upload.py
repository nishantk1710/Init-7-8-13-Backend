"""FR-6: the monthly ZMM065 upload -- parsing, which upload validation reads,
and per-plant run dates. No database; the API round trip is in
``test_zmm065_upload_postgres.py``."""

from datetime import date, datetime, timezone
from io import BytesIO

import openpyxl
import pytest

from app.core.config import Settings
from app.initiatives.i13.config import build_i13_config
from app.initiatives.i13.report_validation import validate_zmm065
from app.initiatives.i13.zmm065_upload import (
    Zmm065UploadError,
    current_uploads,
    parse_month,
    parse_zmm065_workbook,
)
from app.integrations.sap.postgres_reports import Zmm065Row
from app.models.i13_zmm065_upload import Zmm065Upload

HEADER = ["Mat Code", "Material Description", "Plant", "Criticality", "Last GI Dt", "Days", "Stock Type"]


def _workbook(rows, *, data_sheet: str = "Sheet2", title_row: bool = True, header=HEADER) -> BytesIO:
    """A workbook shaped like the delivered reports: a pivot sheet first, then
    the data sheet with a title row above its header."""
    workbook = openpyxl.Workbook()
    workbook.active.title = "Pivot"
    workbook.active.append(["Row Labels", "Count of Mat Code"])
    sheet = workbook.create_sheet(data_sheet)
    if title_row:
        sheet.append(["ZMM065 Aging Report"])
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


def _row(material, plant="1300", stock_type="Fast Moving", last_gi=datetime(2026, 8, 20), days=11):
    return [material, "desc", plant, "C1", last_gi, days, stock_type]


# --- parsing --------------------------------------------------------------


def test_the_data_sheet_is_found_by_its_columns_not_its_name() -> None:
    parsed = parse_zmm065_workbook(_workbook([_row("000000000012345"), _row("678")], data_sheet="Aug data"))
    assert parsed.sheet_name == "Aug data"
    assert parsed.plant == "1300"
    assert [r.material for r in parsed.rows] == ["12345", "678"]  # zero padding stripped, as seeded
    first = parsed.rows[0]
    assert (first.stock_type, first.last_gi_date, first.days) == ("Fast Moving", date(2026, 8, 20), 11)


def test_a_header_on_the_first_row_is_found_too() -> None:
    parsed = parse_zmm065_workbook(_workbook([_row("1")], title_row=False))
    assert len(parsed.rows) == 1


def test_a_sheet_missing_a_required_column_is_refused_by_name() -> None:
    header = [h for h in HEADER if h != "Stock Type"]
    with pytest.raises(Zmm065UploadError, match="stock_type"):
        parse_zmm065_workbook(_workbook([_row("1")[:-1]], header=header))


def test_blank_and_out_of_scope_rows_are_skipped_and_counted() -> None:
    rows = [_row("1"), _row("2", plant="4000"), [None, "Total", None, None, None, None, None]]
    parsed = parse_zmm065_workbook(_workbook(rows))
    assert (len(parsed.rows), parsed.skipped_out_of_scope, parsed.skipped_blank) == (1, 1, 1)


def test_one_upload_is_one_plant() -> None:
    with pytest.raises(Zmm065UploadError, match="one plant at a time"):
        parse_zmm065_workbook(_workbook([_row("1", plant="1300"), _row("2", plant="1500")]))


def test_a_file_with_no_in_scope_rows_is_refused() -> None:
    with pytest.raises(Zmm065UploadError, match="no rows for the in-scope plants"):
        parse_zmm065_workbook(_workbook([_row("1", plant="4000")]))


def test_a_file_that_is_not_a_workbook_is_refused() -> None:
    with pytest.raises(Zmm065UploadError, match="Excel workbook"):
        parse_zmm065_workbook(BytesIO(b"mat_code,plant\n1,1300\n"))


@pytest.mark.parametrize("raw, expected", [("2026-08", date(2026, 8, 1)), ("2026-08-15", date(2026, 8, 1))])
def test_report_month_is_the_first_of_the_month(raw, expected) -> None:
    assert parse_month(raw) == expected


@pytest.mark.parametrize("raw", ["", "August", "2026-13"])
def test_a_report_month_that_is_not_a_month_is_refused(raw) -> None:
    with pytest.raises(Zmm065UploadError):
        parse_month(raw)


# --- which upload validation reads ---------------------------------------


def _upload(id_, plant, month, minute) -> Zmm065Upload:
    return Zmm065Upload(
        id=id_,
        plant=plant,
        report_month=month,
        file_name=f"{id_}.xlsx",
        sheet_name="Sheet1",
        row_count=1,
        uploaded_by="vzi",
        uploaded_at=datetime(2026, 10, 1, 9, minute, tzinfo=timezone.utc),
    )


AUG, SEP = date(2026, 8, 1), date(2026, 9, 1)
# list_uploads order: newest month first, then newest upload first.
UPLOADS = [
    _upload(4, "1300", SEP, 30),  # re-upload of September supersedes 3
    _upload(3, "1300", SEP, 10),
    _upload(2, "1500", AUG, 5),
    _upload(1, "1300", AUG, 1),
]


def test_by_default_each_plant_reads_its_latest_upload() -> None:
    chosen = current_uploads(UPLOADS)
    assert {plant: u.id for plant, u in chosen.items()} == {"1300": 4, "1500": 2}


def test_a_chosen_month_reads_only_that_months_uploads() -> None:
    assert {p: u.id for p, u in current_uploads(UPLOADS, AUG).items()} == {"1300": 1, "1500": 2}
    assert {p: u.id for p, u in current_uploads(UPLOADS, SEP).items()} == {"1300": 4}


# --- per-plant run dates --------------------------------------------------


def test_each_plant_is_classified_as_of_its_own_run_date() -> None:
    """BMM run on 3 Sep, GB on 3 Aug: a last issue on 1 Sep 2025 is 367 days
    old (SLOW) at BMM's date, 336 days (FAST) at GB's. A single shared date
    would misclassify one of them."""
    last = date(2025, 9, 1)
    bmm_date, gb_date = date(2026, 9, 3), date(2026, 8, 3)
    rows = [
        Zmm065Row("A", "1300", "Slow Moving", last, (bmm_date - last).days),
        Zmm065Row("B", "1500", "Fast Moving", last, (gb_date - last).days),
    ]
    result = validate_zmm065(
        rows,
        last_issue_as_of=lambda key, day: last if day >= last else None,
        history_start=date(2025, 8, 1),
        thresholds=build_i13_config(Settings()).aging,
        tolerance_pct=5.0,
    )
    assert (result.compared, result.agreed) == (2, 2)

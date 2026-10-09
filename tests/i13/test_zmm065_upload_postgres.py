"""FR-6: the ZMM065 upload API against real Postgres -- store, duplicate
refusal, replace, and validation reading the upload.

Skipped when no ``DATABASE_URL`` is configured. Every upload written here is
deleted at the end, so the shared database is left as it was found.
"""

from io import BytesIO

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import app
from app.models.i13_zmm065_upload import Zmm065Upload, Zmm065UploadRow

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")
pytestmark = [needs_db]

client = TestClient(app)

#: A month far from any real upload, so these tests never collide with one.
MONTH = "2001-01"
URL = "/api/i13/validation/zmm065/uploads"


def _workbook(plant: str = "1300") -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["ZMM065"])
    sheet.append(["Mat Code", "Plant", "Last GI Dt", "Days", "Stock Type"])
    sheet.append(["TESTZMM0651", plant, "2000-12-01", 31, "Fast Moving"])
    sheet.append(["TESTZMM0652", plant, None, None, "OBSOLETE"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _post(*, replace: bool = False, plant: str = "1300"):
    return client.post(
        URL,
        data={"report_month": MONTH, "replace": str(replace).lower()},
        files={"file": ("ZMM065 test.xlsx", _workbook(plant), "application/octet-stream")},
        headers={"X-Actor-Id": "vzi.tester"},
    )


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with get_sessionmaker()() as db:
        ids = [
            u.id
            for u in db.query(Zmm065Upload).filter(Zmm065Upload.file_name == "ZMM065 test.xlsx").all()
        ]
        if ids:
            db.execute(delete(Zmm065UploadRow).where(Zmm065UploadRow.upload_id.in_(ids)))
            db.execute(delete(Zmm065Upload).where(Zmm065Upload.id.in_(ids)))
            db.commit()


def test_an_upload_is_stored_with_its_uploader_and_plant() -> None:
    response = _post()
    assert response.status_code == 201, response.text
    body = response.json()
    upload = body["upload"]
    assert (upload["plant"], upload["report_month"], upload["row_count"]) == ("1300", "2001-01-01", 2)
    assert upload["uploaded_by"] == "vzi.tester"
    assert upload["report_date"] == "2001-01-01"
    assert body["replaced_earlier"] is False

    listed = client.get(URL).json()
    assert any(u["id"] == upload["id"] for u in listed)


def test_the_same_plant_and_month_is_refused_without_replace_and_kept_with_it() -> None:
    first = _post().json()["upload"]
    duplicate = _post()
    assert duplicate.status_code == 409
    assert "replace" in duplicate.json()["detail"]

    replaced = _post(replace=True)
    assert replaced.status_code == 201
    assert replaced.json()["replaced_earlier"] is True
    listed = {u["id"]: u for u in client.get(URL).json()}
    assert first["id"] in listed, "the earlier upload is kept, never overwritten"


def test_validation_for_the_month_reads_that_upload() -> None:
    upload = _post().json()["upload"]
    body = client.get("/api/i13/validation", params={"report_month": MONTH}).json()
    assert body["zmm065_report_month"] == "2001-01-01"
    assert [s["upload_id"] for s in body["zmm065_sources"]] == [upload["id"]]
    assert body["zmm065"]["rows_in_report"] == 2
    assert body["zmm065"]["excluded_non_aging"] == {"OBSOLETE": 1}


def test_a_month_with_no_upload_is_reference_unavailable() -> None:
    body = client.get("/api/i13/validation", params={"report_month": "2000-01"}).json()
    assert body["zmm065"] is None
    assert body["zmm065_sources"] == []
    assert all(r["status"] == "REFERENCE_UNAVAILABLE" for r in body["results"][:3])


def test_a_file_that_is_not_xlsx_is_refused() -> None:
    response = client.post(
        URL, data={"report_month": MONTH}, files={"file": ("report.csv", b"a,b\n", "text/csv")}
    )
    assert response.status_code == 422

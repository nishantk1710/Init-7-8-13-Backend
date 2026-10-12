"""The /api/criticality routes, over an in-memory database."""

from __future__ import annotations

import io
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.criticality import router as router_module
from app.core.db import get_db
from app.integrations.criticality import reports
from app.main import create_app
from app.models.criticality_report import CriticalityReportUpload
from tests.criticality.conftest import workbook

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class MemoryStorage:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    @contextmanager
    def open_write(self, key):
        buffer = io.BytesIO()
        yield buffer
        self.files[key] = buffer.getvalue()


class BrokenStorage:
    @contextmanager
    def open_write(self, key):
        raise OSError("the lake is unreachable")
        yield  # pragma: no cover


@pytest.fixture
def client(factory):
    app = create_app()

    def _db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    # No ``with``: entering the context runs the lifespan, which touches the
    # real database. These tests are about the response contract.
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def storage(monkeypatch):
    memory = MemoryStorage()
    monkeypatch.setattr(reports, "get_settings", lambda: SimpleNamespace(storage_url="abfss://x@y.dfs.core.windows.net/z"))
    monkeypatch.setattr("app.core.storage.get_storage", lambda: memory)
    return memory


def _post(client, rows, *, plant="1300", month="2026-08", replace=False, actor="nishant", name="aug.xlsx", file_plant=None):
    data = {"plant": plant, "report_month": month, "replace": str(replace).lower()}
    headers = {"X-Actor-Id": actor} if actor else {}
    book = workbook(rows, plant=file_plant or plant)
    return client.post(
        "/api/criticality/uploads", data=data, files={"file": (name, book.getvalue(), XLSX)}, headers=headers
    )


ROWS = [("1", "CRITICAL"), ("2", "NORMAL"), ("3", None)]


def test_an_upload_returns_what_the_page_shows(client) -> None:
    response = _post(client, ROWS)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["stats"] == {
        "rows_read": 3, "tiers": 2, "duplicates": 0, "blank_tiers": 1, "skipped_blank_material": 0,
        "file_plant": "1300",
    }
    assert body["is_current"] is True and body["current_month"] == "2026-08-01"
    assert body["replaced_upload_id"] is None and body["file_stored"] is False
    upload = body["upload"]
    assert (upload["plant"], upload["plant_name"], upload["report_month"]) == ("1300", "Black Mountain Mining", "2026-08-01")
    assert (upload["file_name"], upload["uploaded_by"], upload["row_count"], upload["status"]) == (
        "aug.xlsx", "nishant", 3, "current",
    )


def test_the_uploader_comes_from_the_actor_header(client) -> None:
    assert _post(client, ROWS, actor=None).json()["upload"]["uploaded_by"] == "unknown-actor"


def test_the_same_month_is_a_409_until_replace_is_sent(client) -> None:
    assert _post(client, ROWS).status_code == 201
    conflict = _post(client, ROWS, actor="someone-else")
    assert conflict.status_code == 409
    assert "already has a report for August 2026" in conflict.json()["detail"]
    replaced = _post(client, [("1", "IMPACT")], replace=True, actor="someone-else", name="aug-v2.xlsx")
    assert replaced.status_code == 201
    assert replaced.json()["replaced_upload_id"] is not None
    history = client.get("/api/criticality/uploads").json()
    assert {(h["file_name"], h["status"]) for h in history} == {("aug-v2.xlsx", "current"), ("aug.xlsx", "replaced")}


def test_a_back_dated_month_is_stored_but_not_current(client) -> None:
    _post(client, ROWS, month="2026-08")
    july = _post(client, ROWS, month="2026-07", name="jul.xlsx").json()
    assert july["is_current"] is False and july["current_month"] == "2026-08-01"
    assert july["upload"]["status"] == "previous"


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"file_plant": "1500"}, "This file is for plant 1500 (3 rows), but you picked 1300"),
        ({"plant": "4000", "file_plant": "4000"}, "is not one of the platform's plants"),
        ({"month": "August"}, "is not a month"),
        ({"month": "2999-01"}, "has not started yet"),
        ({"name": "report.csv"}, "is not an Excel workbook"),
    ],
)
def test_a_report_that_cannot_be_used_is_a_422_with_a_sentence(client, kwargs, message) -> None:
    response = _post(client, ROWS, **kwargs)
    assert response.status_code == 422
    assert message in response.json()["detail"]


def test_an_unknown_tier_is_a_422(client) -> None:
    response = _post(client, [("1", "CRITCAL")])
    assert response.status_code == 422 and "Unrecognised: 'CRITCAL'" in response.json()["detail"]


def test_a_garbage_file_is_a_422(client) -> None:
    response = client.post(
        "/api/criticality/uploads",
        data={"plant": "1300", "report_month": "2026-08"},
        files={"file": ("a.xlsx", b"not a workbook", XLSX)},
    )
    assert response.status_code == 422 and "Excel workbook" in response.json()["detail"]


def test_an_oversized_file_is_a_413(client, monkeypatch) -> None:
    monkeypatch.setattr(router_module, "MAX_UPLOAD_BYTES", 10)
    assert _post(client, ROWS).status_code == 413


def test_nothing_is_stored_for_a_refused_upload(client, factory) -> None:
    _post(client, ROWS, file_plant="1500")
    with factory() as session:
        assert session.scalars(select(CriticalityReportUpload)).all() == []


def test_the_original_file_is_kept_when_storage_is_configured(client, storage, factory) -> None:
    body = _post(client, ROWS, name="BMM Aug (final).xlsx").json()
    assert body["file_stored"] is True
    [(key, data)] = storage.files.items()
    assert key.startswith("criticality-reports/1300/2026-08/") and key.endswith("-BMM_Aug_final_.xlsx")
    assert data[:2] == b"PK"  # the xlsx, byte for byte
    with factory() as session:
        upload = session.scalars(select(CriticalityReportUpload)).one()
        assert upload.storage_key == key and len(upload.sha256) == 64


def test_a_failed_archive_copy_does_not_refuse_the_report(client, monkeypatch) -> None:
    monkeypatch.setattr(reports, "get_settings", lambda: SimpleNamespace(storage_url="abfss://x@y.dfs.core.windows.net/z"))
    monkeypatch.setattr("app.core.storage.get_storage", lambda: BrokenStorage())
    body = _post(client, ROWS).json()
    assert body["file_stored"] is False and body["upload"]["status"] == "current"


def test_a_duplicate_is_refused_before_the_file_is_archived(client, storage) -> None:
    _post(client, ROWS)
    _post(client, ROWS)  # 409
    assert len(storage.files) == 1


def test_the_plants_list_reports_each_plants_current_month(client) -> None:
    assert client.get("/api/criticality/plants").json() == [
        {"code": "1300", "name": "Black Mountain Mining", "current_month": None, "materials": 0},
        {"code": "1500", "name": "Gamsberg", "current_month": None, "materials": 0},
    ]
    _post(client, ROWS, plant="1500")
    plants = {p["code"]: p for p in client.get("/api/criticality/plants").json()}
    assert (plants["1500"]["current_month"], plants["1500"]["materials"]) == ("2026-08-01", 3)
    assert plants["1300"]["current_month"] is None


def test_history_can_be_narrowed_to_a_plant(client) -> None:
    _post(client, ROWS, plant="1300")
    _post(client, ROWS, plant="1500", name="gb.xlsx")
    assert [h["plant"] for h in client.get("/api/criticality/uploads?plant=1500").json()] == ["1500"]
    assert len(client.get("/api/criticality/uploads").json()) == 2

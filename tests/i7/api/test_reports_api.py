"""I07 Quarterly Deep-Dive Report -- API endpoints.

Real Postgres or skip, using the application's real TestClient (mirrors
``tests/i7/api/test_i7_api.py``). Uses a throwaway far-future quarter
(``"Q1 2099"``) so tests never collide with, or leave behind, a real
generated report; cleaned up in a fixture teardown.

Generation is synchronous and takes tens of seconds against the full
dataset (documented in ``app/api/i7/reports.py``) -- these tests accept that
cost rather than mocking it away, since the whole point is to prove the real
aggregation-plus-persistence path end to end.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import app
from app.models.i7_reporting import QuarterlyReportRecord

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")

client = TestClient(app)

TEST_QUARTER = "Q1 2099"


@pytest.fixture
def clean_test_quarter():
    with get_sessionmaker()() as session:
        session.execute(delete(QuarterlyReportRecord).where(QuarterlyReportRecord.quarter == TEST_QUARTER))
        session.commit()
    yield TEST_QUARTER
    with get_sessionmaker()() as session:
        session.execute(delete(QuarterlyReportRecord).where(QuarterlyReportRecord.quarter == TEST_QUARTER))
        session.commit()


# --- generate -----------------------------------------------------------------


@needs_db
def test_generate_returns_200_with_a_full_report_body(clean_test_quarter):
    quarter = clean_test_quarter
    response = client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": quarter})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "metadata", "executive_summary", "scope_and_data_quality", "demand_classification",
        "forecasting", "safety_stock", "reorder_point", "max_stock", "oar", "recommendations",
        "approval", "baseline_comparison", "sap_adoption", "limitations",
        "material_criticality", "management_summary",
    }
    assert body["metadata"]["quarter"] == quarter


@needs_db
def test_generate_with_an_invalid_quarter_returns_a_clean_400_not_a_500():
    response = client.post(
        "/api/v1/i7/reports/quarterly/generate", json={"quarter": "not-a-real-quarter"}
    )
    assert response.status_code == 400
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "INVALID_QUARTER"


@needs_db
def test_generate_called_twice_is_idempotent_no_duplicate_row(clean_test_quarter):
    quarter = clean_test_quarter
    first = client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": quarter})
    assert first.status_code == 200
    second = client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": quarter})
    assert second.status_code == 200

    with get_sessionmaker()() as session:
        from sqlalchemy import func, select

        count = session.execute(
            select(func.count()).select_from(QuarterlyReportRecord).where(
                QuarterlyReportRecord.quarter == quarter
            )
        ).scalar_one()
    assert count == 1

    # Second call's own generated_at must be >= the first's -- a fresh
    # regeneration, not a stale cached response.
    assert second.json()["metadata"]["generated_at"] >= first.json()["metadata"]["generated_at"]


# --- get ------------------------------------------------------------------------


@needs_db
def test_get_after_generate_returns_the_same_report(clean_test_quarter):
    quarter = clean_test_quarter
    generated = client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": quarter})
    assert generated.status_code == 200

    fetched = client.get(f"/api/v1/i7/reports/quarterly/{quarter}")
    assert fetched.status_code == 200
    assert fetched.json()["executive_summary"] == generated.json()["executive_summary"]


@needs_db
def test_get_of_a_never_generated_quarter_returns_404():
    response = client.get("/api/v1/i7/reports/quarterly/Q2 2099")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REPORT_NOT_FOUND"


# --- list -----------------------------------------------------------------------


@needs_db
def test_list_returns_200_with_pagination_shape(clean_test_quarter):
    quarter = clean_test_quarter
    client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": quarter})

    response = client.get("/api/v1/i7/reports/quarterly")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "total"}
    assert body["total"] == len(body["items"])
    quarters = [item["quarter"] for item in body["items"]]
    assert quarter in quarters


@needs_db
def test_list_items_never_carry_report_json(clean_test_quarter):
    quarter = clean_test_quarter
    client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": quarter})

    body = client.get("/api/v1/i7/reports/quarterly").json()
    for item in body["items"]:
        assert "report_json" not in item


@needs_db
def test_list_respects_the_limit_query_param():
    response = client.get("/api/v1/i7/reports/quarterly?limit=1")
    assert response.status_code == 200
    assert len(response.json()["items"]) <= 1


@needs_db
def test_list_rejects_an_out_of_range_limit():
    response = client.get("/api/v1/i7/reports/quarterly?limit=9999")
    assert response.status_code == 422


# --- status -----------------------------------------------------------------------


@needs_db
def test_status_of_a_never_generated_quarter_is_pending():
    response = client.get("/api/v1/i7/reports/quarterly/Q3 2099/status")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["report_id"] is None


@needs_db
def test_status_after_generate_is_completed(clean_test_quarter):
    quarter = clean_test_quarter
    client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": quarter})

    response = client.get(f"/api/v1/i7/reports/quarterly/{quarter}/status")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["report_id"] is not None
    assert body["error"] is None


# --- export -----------------------------------------------------------------------


@needs_db
def test_export_of_a_generated_quarter_returns_an_xlsx_stream(clean_test_quarter):
    quarter = clean_test_quarter
    client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": quarter})

    response = client.get(f"/api/v1/i7/reports/quarterly/{quarter}/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert len(response.content) > 0


@needs_db
def test_export_of_a_never_generated_quarter_returns_404():
    response = client.get("/api/v1/i7/reports/quarterly/Q4 2099/export")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REPORT_NOT_FOUND"


# --- route registration order ------------------------------------------------


@needs_db
def test_quarterly_list_route_is_not_swallowed_by_the_quarter_path(clean_test_quarter):
    """'quarterly' (the list route) must never be read as a quarter value by
    the /{quarter} catch-all -- registered first per the router's own
    docstring."""
    response = client.get("/api/v1/i7/reports/quarterly")
    assert response.status_code == 200
    assert "metadata" not in response.json()


@needs_db
def test_error_envelope_is_consistent_for_report_errors():
    response = client.get("/api/v1/i7/reports/quarterly/Q1 1900")
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}

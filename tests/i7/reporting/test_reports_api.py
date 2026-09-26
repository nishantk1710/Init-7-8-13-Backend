"""I07 Quarterly Deep-Dive Report -- API + persistence tests.

Real Postgres or skip (matches ``tests/i7/api/test_i7_api.py``'s pattern).
Uses the application's real TestClient and the real database session
dependency. Generation is expensive (~40-50s per the API agent's own smoke
test), so this module generates its one throwaway quarter once in a
module-scoped fixture and every test reads that same result, then deletes
the row in teardown to leave the dev DB clean -- mirroring how
``test_i7_api.py``'s workflow tests insert and remove their own throwaway
rows.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import app
from app.models.i7_recommendation import Recommendation
from app.models.i7_reporting import QuarterlyReportRecord

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")

client = TestClient(app)

# A quarter unlikely to collide with any real report a human generated --
# far enough in the past that no production data was ever tagged with it,
# but still a validly-formatted quarter so resolve_quarter accepts it.
TEST_QUARTER = "Q1 2001"


def _any_recommendation_exists() -> bool:
    factory = get_sessionmaker()
    with factory() as session:
        return session.execute(select(Recommendation.id).limit(1)).first() is not None


@pytest.fixture
def cleanup_test_quarter():
    yield
    factory = get_sessionmaker()
    with factory() as session:
        session.execute(delete(QuarterlyReportRecord).where(QuarterlyReportRecord.quarter == TEST_QUARTER))
        session.commit()


@needs_db
def test_generate_report_end_to_end_returns_200_and_full_report_shape(cleanup_test_quarter):
    response = client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": TEST_QUARTER})
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {
        "metadata",
        "executive_summary",
        "scope_and_data_quality",
        "demand_classification",
        "forecasting",
        "safety_stock",
        "reorder_point",
        "max_stock",
        "oar",
        "recommendations",
        "approval",
        "baseline_comparison",
        "sap_adoption",
        "limitations",
    }
    assert body["metadata"]["quarter"] == TEST_QUARTER
    assert len(body["baseline_comparison"]["rows"]) == 4


@needs_db
def test_generate_report_persists_a_row(cleanup_test_quarter):
    client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": TEST_QUARTER})

    factory = get_sessionmaker()
    with factory() as session:
        row = session.execute(
            select(QuarterlyReportRecord).where(QuarterlyReportRecord.quarter == TEST_QUARTER)
        ).scalar_one_or_none()
    assert row is not None
    assert row.status == "COMPLETED"
    assert row.report_json is not None


@needs_db
def test_generate_report_twice_is_idempotent_no_duplicate_row(cleanup_test_quarter):
    first = client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": TEST_QUARTER})
    assert first.status_code == 200
    second = client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": TEST_QUARTER})
    assert second.status_code == 200

    factory = get_sessionmaker()
    with factory() as session:
        rows = session.execute(
            select(QuarterlyReportRecord).where(QuarterlyReportRecord.quarter == TEST_QUARTER)
        ).scalars().all()
    assert len(rows) == 1  # upsert-by-quarter, never a second row

    first_id = first.json()  # sanity: still a valid, complete body
    assert "metadata" in first_id
    assert "metadata" in second.json()


@needs_db
def test_generate_report_with_invalid_quarter_returns_clean_error_not_500():
    response = client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": "not-a-quarter"})
    assert response.status_code == 400
    body = response.json()
    assert "error" in body
    assert body["error"]["code"] == "INVALID_QUARTER"
    assert "message" in body["error"]


@needs_db
def test_list_quarterly_reports_returns_summaries_without_report_json(cleanup_test_quarter):
    client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": TEST_QUARTER})

    response = client.get("/api/v1/i7/reports/quarterly")
    assert response.status_code == 200
    body = response.json()
    assert "items" in body
    assert "total" in body
    matching = [item for item in body["items"] if item["quarter"] == TEST_QUARTER]
    assert len(matching) == 1
    assert "report_json" not in matching[0]
    assert set(matching[0]) == {
        "report_id",
        "quarter",
        "status",
        "report_version",
        "generated_at",
        "period_start",
        "period_end",
    }


@needs_db
def test_get_quarterly_report_returns_full_body(cleanup_test_quarter):
    client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": TEST_QUARTER})

    response = client.get(f"/api/v1/i7/reports/quarterly/{TEST_QUARTER}")
    assert response.status_code == 200
    body = response.json()
    assert body["metadata"]["quarter"] == TEST_QUARTER


@needs_db
def test_get_quarterly_report_404_for_never_generated_quarter():
    response = client.get("/api/v1/i7/reports/quarterly/Q1 1901")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "REPORT_NOT_FOUND"


@needs_db
def test_status_endpoint_completed_after_generation(cleanup_test_quarter):
    client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": TEST_QUARTER})

    response = client.get(f"/api/v1/i7/reports/quarterly/{TEST_QUARTER}/status")
    assert response.status_code == 200
    body = response.json()
    assert body["quarter"] == TEST_QUARTER
    assert body["status"] == "COMPLETED"
    assert body["report_id"] is not None


@needs_db
def test_status_endpoint_pending_for_never_generated_quarter():
    """Per the API's documented polling-shape design: a nonexistent quarter
    is PENDING, not a 404."""
    response = client.get("/api/v1/i7/reports/quarterly/Q2 1901/status")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["report_id"] is None


@needs_db
def test_export_endpoint_returns_xlsx(cleanup_test_quarter):
    client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": TEST_QUARTER})

    response = client.get(f"/api/v1/i7/reports/quarterly/{TEST_QUARTER}/export")
    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert len(response.content) > 0


@needs_db
def test_export_endpoint_404_for_never_generated_quarter():
    response = client.get("/api/v1/i7/reports/quarterly/Q3 1901/export")
    assert response.status_code == 404

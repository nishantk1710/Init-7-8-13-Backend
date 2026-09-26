"""End-to-end smoke tests for the W6.6 ACT API against real Postgres data.

Skipped outright when no ``DATABASE_URL`` is configured -- same convention
as ``tests/i13/test_api.py``. Kept deliberately conservative about what real
seeded data contains (no assumption that a PLAN_BREACH/NO_PLAN condition
exists yet): the detailed exception-rule behaviour is covered by
``tests/i13/test_act_service.py``'s fake-repository unit tests, which need
no database at all. What this file adds is the thing only a real
Postgres-backed request path can prove: routing, 404s and idempotency
through the real ``get_db``/``SqlExceptionRepository`` wiring.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")
pytestmark = [needs_db, pytest.mark.needs_seed_data]

client = TestClient(app)


def test_act_utilisation_list_reads_the_mart() -> None:
    response = client.get("/api/i13/act/utilisation", params={"plant": "1300"})
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_act_utilisation_detail_unknown_material_plant_is_404() -> None:
    response = client.get("/api/i13/act/utilisation/DOES-NOT-EXIST/0000")
    assert response.status_code == 404


def test_act_exceptions_list_returns_200() -> None:
    response = client.get("/api/i13/act/exceptions", params={"plant": "1300"})
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_act_exceptions_invalid_status_filter_is_400() -> None:
    response = client.get("/api/i13/act/exceptions", params={"status": "NOT_A_REAL_STATUS"})
    assert response.status_code == 400


def test_act_exception_detail_unknown_is_404() -> None:
    response = client.get("/api/i13/act/exceptions/ACT-DOES-NOT-EXIST")
    assert response.status_code == 404


def test_act_exception_history_unknown_is_404() -> None:
    response = client.get("/api/i13/act/exceptions/ACT-DOES-NOT-EXIST/history")
    assert response.status_code == 404


def test_act_confirmation_on_unknown_exception_is_404() -> None:
    response = client.post(
        "/api/i13/act/exceptions/ACT-DOES-NOT-EXIST/confirmation",
        json={"reason_category": "OTHER", "free_text": "test"},
        headers={"X-Actor-Id": "TEST-REQ"},
    )
    assert response.status_code == 404


def test_run_detect_then_run_escalate_are_callable_independently() -> None:
    """Both application operations are plain HTTP-triggerable calls today
    (no scheduler exists yet -- see app.initiatives.i13.act.service's
    docstring); this proves they work end to end against real data without
    asserting anything about what real data happens to contain."""
    detect_response = client.post("/api/i13/act/run/detect", json={"plant": "1300"})
    assert detect_response.status_code == 200
    body = detect_response.json()
    assert set(body) == {"as_of_time", "created", "reused", "resolved", "routed"}

    escalate_response = client.post("/api/i13/act/run/escalate", json={})
    assert escalate_response.status_code == 200
    escalate_body = escalate_response.json()
    assert set(escalate_body) == {"as_of_time", "escalated", "routing_pending"}


def test_run_detect_is_idempotent_against_unchanged_source_data() -> None:
    as_of_time = "2026-09-18T00:00:00Z"
    first = client.post("/api/i13/act/run/detect", json={"plant": "1300", "as_of_time": as_of_time})
    assert first.status_code == 200
    second = client.post("/api/i13/act/run/detect", json={"plant": "1300", "as_of_time": as_of_time})
    assert second.status_code == 200
    # Nothing changed between the two calls -- the second run must create no
    # new exceptions (see app.initiatives.i13.act.service's dedup rule).
    assert second.json()["created"] == 0

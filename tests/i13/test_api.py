"""End-to-end smoke tests for the I13 API against real Postgres data.

Skipped outright when no ``DATABASE_URL`` is configured. Exercises the app's
real ``get_db`` dependency (``app.core.db``, which accepts Postgres directly
-- see that module's docstring) rather than bypassing it, so this is a
genuine end-to-end path: the same session wiring a real client request uses.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")
pytestmark = [needs_db, pytest.mark.needs_seed_data]

client = TestClient(app)


def test_legacy_ledger_list_and_round_trip() -> None:
    """GET /api/i13/ledger -- the compatibility shim for the pre-migration
    frontend contract (see app.initiatives.i13.ledger_compat)."""
    response = client.get("/api/i13/ledger", params={"plant": "1300"})
    assert response.status_code == 200
    entries = response.json()
    assert len(entries) > 0

    ledger_id = entries[0]["ledger_id"]
    round_trip = client.get(f"/api/i13/ledger/{ledger_id}")
    assert round_trip.status_code == 200
    assert round_trip.json()["ledger_id"] == ledger_id


def test_legacy_ledger_unknown_id_is_404() -> None:
    response = client.get("/api/i13/ledger/does-not-exist")
    assert response.status_code == 404


def test_data_sources_reports_loaded_tables() -> None:
    response = client.get("/api/i13/data-sources")
    assert response.status_code == 200
    statuses = {item["table"]: item for item in response.json()}
    assert statuses["raw_resb"]["status"] == "LOADED"
    assert statuses["raw_resb"]["row_count"] > 0


def test_summary_counts_are_internally_consistent() -> None:
    response = client.get("/api/i13/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["total_oar_positions"] > 0
    assert (
        body["fast_moving_count"] + body["slow_moving_count"] + body["non_moving_count"]
        == body["total_oar_positions"]
    )


def test_movement_metrics_list_and_filter() -> None:
    response = client.get("/api/i13/movement-metrics", params={"plant": "1300", "limit": 5})
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) > 0
    assert all(row["plant"] == "1300" for row in rows)


def test_partial_ledger_list_and_diagnostics() -> None:
    response = client.get("/api/i13/utilisation-ledger/partial", params={"plant": "1300", "limit": 5})
    assert response.status_code == 200
    assert len(response.json()) > 0

    diagnostics = client.get("/api/i13/utilisation-ledger/partial/diagnostics", params={"plant": "1300"})
    assert diagnostics.status_code == 200
    body = diagnostics.json()
    assert (
        body["pr_items_with_no_po"] + body["pr_items_with_single_po"] + body["pr_items_with_multiple_po"]
        == body["pr_items_total"]
    )


def test_reservation_ledger_list_and_round_trip() -> None:
    response = client.get("/api/i13/utilisation-ledger", params={"plant": "1300", "limit": 5})
    assert response.status_code == 200
    entries = response.json()
    assert len(entries) > 0

    entry = entries[0]
    round_trip = client.get(f"/api/i13/utilisation-ledger/{entry['reservation_number']}/{entry['reservation_item']}")
    assert round_trip.status_code == 200
    assert round_trip.json()["reservation_number"] == entry["reservation_number"]


def test_reservation_ledger_unknown_id_is_404() -> None:
    response = client.get("/api/i13/utilisation-ledger/does-not-exist/0")
    assert response.status_code == 404


def test_consumption_attribution_list_and_round_trip() -> None:
    response = client.get("/api/i13/consumption-attribution", params={"plant": "1300", "limit": 5})
    assert response.status_code == 200
    entries = response.json()
    assert len(entries) > 0
    assert all(e["status"] in ("ATTRIBUTED", "PARTIALLY_ATTRIBUTED", "UNATTRIBUTED", "AMBIGUOUS") for e in entries)

    entry = entries[0]
    round_trip = client.get(
        f"/api/i13/consumption-attribution/{entry['reservation_number']}/{entry['reservation_item']}"
    )
    assert round_trip.status_code == 200
    assert round_trip.json()["reservation_number"] == entry["reservation_number"]


def test_consumption_attribution_unknown_id_is_404() -> None:
    response = client.get("/api/i13/consumption-attribution/does-not-exist/0")
    assert response.status_code == 404


def test_consumption_attribution_cost_centre_disabled_by_default() -> None:
    response = client.get("/api/i13/consumption-attribution", params={"plant": "1300", "limit": 20})
    assert response.status_code == 200
    entries = response.json()
    assert len(entries) > 0
    assert all(e["cost_centre"] is None for e in entries)
    assert all(e["cost_centre_attribution_enabled"] is False for e in entries)


def test_watch_returns_insufficient_history_rather_than_zero() -> None:
    response = client.get("/api/i13/watch", params={"plant": "1300"})
    assert response.status_code == 200
    metrics = response.json()
    assert any(m["months_of_cover"] is None and m["months_of_cover_reason"] == "INSUFFICIENT_HISTORY" for m in metrics)


def test_exceptions_only_contains_i13_exception_types() -> None:
    response = client.get("/api/i13/exceptions")
    assert response.status_code == 200
    types = {item["type"] for item in response.json()}
    assert types <= {"PLAN_BREACH", "NO_PLAN", "GR_NOT_ISSUED_30_DAY"}


def test_reclassification_candidates_use_real_criticality_but_never_fabricate_hod_justification() -> None:
    response = client.get("/api/i13/reclassification", params={"plant": "1300"})
    assert response.status_code == 200
    candidates = response.json()
    assert len(candidates) > 0
    # No HOD-justification workflow (W6.6) exists in this codebase yet, so
    # that indicator -- and therefore data_available (which requires every
    # indicator to have actually resolved) -- must never be fabricated.
    assert all(c["hod_justified_request_indicator"] is None for c in candidates)
    assert all(c["data_available"] is False for c in candidates)
    # Criticality (W3.4) IS wired in and resolves real tiers from the seeded
    # ZMM065 data at plant 1300 -- unlike the old stub, this is not
    # universally None.
    assert any(c["critical_impact_indicator"] is not None for c in candidates)


def test_validation_reports_reference_unavailable_without_reference_counts() -> None:
    response = client.get("/api/i13/validation")
    assert response.status_code == 200
    body = response.json()
    assert all(result["status"] == "REFERENCE_UNAVAILABLE" for result in body["results"])


def test_validation_reconciles_when_reference_provided() -> None:
    ledger_count = len(client.get("/api/i13/utilisation-ledger/partial", params={"limit": 1000}).json())
    response = client.get("/api/i13/validation", params={"zmm065_reference_count": ledger_count})
    body = response.json()
    zmm065 = next(r for r in body["results"] if r["source_name"] == "ZMM065")
    # A partial page (limit=1000) will not equal the true unfiltered count,
    # so this only proves the reconciliation math runs end to end -- exact
    # RECONCILED/OUT_OF_TOLERANCE depends on how many procurement lines
    # exist beyond the page, which is a real, changing number.
    assert zmm065["status"] in ("RECONCILED", "OUT_OF_TOLERANCE")

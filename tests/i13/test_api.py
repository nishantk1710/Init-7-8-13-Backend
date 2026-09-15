"""End-to-end smoke tests for the I13 API against the real generated dataset.

Every other I13 test builds tiny synthetic CSVs; this file is the one path
that exercises the full stack against ``data-generator/generated`` the way
the frontend will.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_data_sources_reports_live_and_mock_modes() -> None:
    response = client.get("/api/i13/data-sources")
    assert response.status_code == 200
    statuses = {item["entity_set"]: item for item in response.json()}

    assert statuses["PurchaseRequisitionSet"]["mode"] == "LIVE"
    assert statuses["GoodsMovementItemSet"]["mode"] == "LIVE"
    assert statuses["ReservationItemSet"]["mode"] == "MOCK"
    assert statuses["MaterialValuationSet"]["mode"] == "MOCK"
    assert statuses["MonthlyMovementStatisticSet"]["mode"] == "MOCK"
    assert statuses["MonthlyMovementStatisticSet"]["available"] is False


def test_summary_counts_are_internally_consistent() -> None:
    response = client.get("/api/i13/summary")
    assert response.status_code == 200
    body = response.json()
    assert body["total_oar_positions"] > 0
    assert (
        body["fast_moving_count"] + body["slow_moving_count"] + body["non_moving_count"]
        == body["total_oar_positions"]
    )
    assert body["valuation_is_mocked"] is True


def test_ledger_list_and_filters() -> None:
    response = client.get("/api/i13/ledger")
    assert response.status_code == 200
    entries = response.json()
    assert len(entries) > 0

    plant = entries[0]["plant"]
    filtered = client.get("/api/i13/ledger", params={"plant": plant})
    assert filtered.status_code == 200
    assert all(entry["plant"] == plant for entry in filtered.json())


def test_ledger_defaults_to_oar_scope_only() -> None:
    """W2.4: the ledger's default view excludes Min-Max/Excluded materials --
    the I13 boundary rule, applied once here rather than by each caller."""
    scoped = client.get("/api/i13/ledger").json()
    unscoped = client.get("/api/i13/ledger", params={"include_out_of_scope": True}).json()
    assert len(unscoped) >= len(scoped)
    assert len(unscoped) > len(scoped), "fixture data should contain at least one non-OAR PR line"


def test_ledger_by_id_round_trips() -> None:
    ledger_id = client.get("/api/i13/ledger").json()[0]["ledger_id"]
    response = client.get(f"/api/i13/ledger/{ledger_id}")
    assert response.status_code == 200
    assert response.json()["ledger_id"] == ledger_id


def test_ledger_unknown_id_is_404() -> None:
    response = client.get("/api/i13/ledger/does-not-exist")
    assert response.status_code == 404


def test_watch_returns_insufficient_history_rather_than_zero() -> None:
    response = client.get("/api/i13/watch")
    assert response.status_code == 200
    metrics = response.json()
    assert any(m["months_of_cover"] is None and m["months_of_cover_reason"] == "INSUFFICIENT_HISTORY" for m in metrics)


def test_exceptions_only_contains_i13_exception_types() -> None:
    response = client.get("/api/i13/exceptions")
    assert response.status_code == 200
    types = {item["type"] for item in response.json()}
    assert types <= {"PLAN_BREACH", "NO_PLAN", "GR_NOT_ISSUED_30_DAY"}


def test_reclassification_candidates_never_fabricate_criticality() -> None:
    response = client.get("/api/i13/reclassification")
    assert response.status_code == 200
    candidates = response.json()
    assert len(candidates) > 0
    assert all(c["data_available"] is False for c in candidates)
    assert all(c["critical_impact_indicator"] is None for c in candidates)


def test_validation_reports_reference_unavailable_without_reference_counts() -> None:
    response = client.get("/api/i13/validation")
    assert response.status_code == 200
    body = response.json()
    assert all(result["status"] == "REFERENCE_UNAVAILABLE" for result in body["results"])


def test_validation_reconciles_when_reference_provided() -> None:
    # /api/i13/validation reconciles against the full (unfiltered) ledger --
    # ZMM065 is a plant-wide aging report, not an OAR-scoped one -- so the
    # reference count must come from the unscoped view, not the ledger
    # endpoint's OAR-only default (see test_ledger_defaults_to_oar_scope_only).
    ledger_count = len(client.get("/api/i13/ledger", params={"include_out_of_scope": True}).json())
    response = client.get("/api/i13/validation", params={"zmm065_reference_count": ledger_count})
    body = response.json()
    zmm065 = next(r for r in body["results"] if r["source_name"] == "ZMM065")
    assert zmm065["status"] == "RECONCILED"
    assert zmm065["within_tolerance"] is True

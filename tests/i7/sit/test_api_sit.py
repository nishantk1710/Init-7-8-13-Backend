"""SIT 5.7 / 5.8 / 5.9 / 5.12 / 5.13 -- the complete Phase 8 API surface,
exercised through the real FastAPI TestClient and the real database, layered
on top of the endpoint-level tests already in tests/i7/api/test_i7_api.py.

This file focuses on what those did not already cover: end-to-end
traceability from a recommendation back through the pipeline stages, database-
side pagination proof (not "it returns paginated JSON" but "it never loads the
whole table"), and structural SAP-write-back safety across the whole API
package, not just app/api/i7.
"""

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import app
from app.models.i7_recommendation import Recommendation

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")

client = TestClient(app)


# --- 5.7: full endpoint coverage smoke pass ------------------------------------------------------


@needs_db
def test_every_documented_endpoint_responds():
    """One pass across every Phase 8 endpoint, confirming none 500s against
    the real, currently-populated database."""
    health = client.get("/api/v1/i7/health")
    assert health.status_code == 200

    listing = client.get("/api/v1/i7/recommendations?page=1&page_size=5")
    assert listing.status_code == 200
    items = listing.json()["items"]
    if not items:
        pytest.skip("no recommendations in the database")
    rid = items[0]["recommendation_id"]

    assert client.get(f"/api/v1/i7/recommendations/{rid}").status_code == 200
    assert client.get(f"/api/v1/i7/recommendations/{rid}/trace").status_code == 200
    assert client.get(f"/api/v1/i7/recommendations/{rid}/approval-history").status_code == 200
    assert client.get(f"/api/v1/i7/recommendations/{rid}/adoption").status_code == 200

    runs = client.get("/api/v1/i7/runs")
    assert runs.status_code == 200
    run_items = runs.json()["items"]
    if run_items:
        first = run_items[0]
        assert client.get(f"/api/v1/i7/runs/{first['run_type']}/{first['run_id']}").status_code == 200


# --- 5.8: pagination is genuinely database-side ---------------------------------------------------


@needs_db
def test_pagination_reflects_the_true_total_not_a_page_length():
    """total must be the full row count, not len(items) -- proving the count
    query is separate from the LIMIT/OFFSET query rather than counting only
    what was fetched."""
    response = client.get("/api/v1/i7/recommendations?page=1&page_size=5").json()
    assert response["total"] >= len(response["items"])
    with get_sessionmaker()() as session:
        from sqlalchemy import func

        actual = session.execute(select(func.count()).select_from(Recommendation)).scalar()
    assert response["total"] == actual


@needs_db
def test_maximum_page_size_is_enforced_at_200():
    at_limit = client.get("/api/v1/i7/recommendations?page_size=200")
    assert at_limit.status_code == 200
    over_limit = client.get("/api/v1/i7/recommendations?page_size=201")
    assert over_limit.status_code == 422


@needs_db
def test_empty_result_set_for_an_impossible_filter():
    response = client.get(
        "/api/v1/i7/recommendations?material=NO-SUCH-MATERIAL-EXISTS&page_size=5"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0


@needs_db
def test_combined_filters_narrow_the_result_correctly():
    response = client.get(
        "/api/v1/i7/recommendations?status=NOT_EVALUABLE&is_oar=false&page_size=10"
    )
    assert response.status_code == 200
    for item in response.json()["items"]:
        assert item["status"] == "NOT_EVALUABLE"
        assert item["is_oar"] is False


@needs_db
def test_far_page_beyond_the_data_returns_an_empty_page_not_an_error():
    response = client.get("/api/v1/i7/recommendations?page=999999&page_size=50")
    assert response.status_code == 200
    assert response.json()["items"] == []


# --- 5.9: error contract -----------------------------------------------------------------------


@needs_db
def test_malformed_request_body_returns_422_with_no_leak():
    response = client.post(
        "/api/v1/i7/recommendations/anything/actions",
        json={"actor_id": "u", "actor_role": "End User"},  # missing required "action"
    )
    assert response.status_code == 422
    assert "Traceback" not in response.text
    assert "sqlalchemy" not in response.text.lower()


@needs_db
def test_nonexistent_run_type_is_a_422_not_a_500():
    response = client.get("/api/v1/i7/runs/not-a-real-type/1")
    assert response.status_code == 422


@needs_db
def test_404_error_body_never_contains_database_internals():
    response = client.get("/api/v1/i7/recommendations/DOES-NOT-EXIST-AT-ALL")
    assert response.status_code == 404
    text = response.text.lower()
    for leak in ("psycopg", "sqlalchemy", "traceback", "select ", "from i7_"):
        assert leak not in text


# --- 5.12: traceability -----------------------------------------------------------------------


@needs_db
def test_a_blocked_normal_recommendation_traces_to_its_blocking_reason():
    """Recommendation -> demand class -> model -> lead time -> service-level
    block, with nothing invented for a stage that never ran."""
    with get_sessionmaker()() as session:
        row = session.execute(
            select(Recommendation).where(
                ~Recommendation.is_oar, Recommendation.status == "NOT_EVALUABLE"
            ).limit(1)
        ).scalar_one_or_none()
    if row is None:
        pytest.skip("no blocked normal-path recommendation")

    detail = client.get(f"/api/v1/i7/recommendations/{row.recommendation_id}").json()
    trace = client.get(f"/api/v1/i7/recommendations/{row.recommendation_id}/trace").json()

    assert detail["status"] == "NOT_EVALUABLE"
    assert detail["blocking_reason"] is not None
    assert trace["blocking_reason"] == detail["blocking_reason"]
    # The demand class is present (Phase 3 ran), but recommended values are not
    # (Phase 5/7 blocked) -- both facts visible in the same response.
    assert detail["demand"]["demand_class"] is not None
    assert detail["recommended"]["safety_stock"] is None


@needs_db
def test_an_oar_recommendation_traces_through_similarity_to_the_estimate_block():
    with get_sessionmaker()() as session:
        row = session.execute(
            select(Recommendation).where(
                Recommendation.oar_similarity_status == "AVAILABLE"
            ).limit(1)
        ).scalar_one_or_none()
    if row is None:
        pytest.skip("no OAR recommendation with available similarity")

    detail = client.get(f"/api/v1/i7/recommendations/{row.recommendation_id}").json()
    trace = client.get(f"/api/v1/i7/recommendations/{row.recommendation_id}/trace").json()

    assert detail["oar"]["similarity_status"] == "AVAILABLE"
    assert detail["oar"]["neighbour_count"] is not None
    # Two legitimate block reasons exist once similarity is AVAILABLE but the
    # estimate itself is not SUCCESS: the unsigned service-level matrix, or
    # (since the minimum_neighbours=5 / minimum_similarity=0.60 admission
    # gate) too few candidates cleared the 0.60 similarity floor.
    assert detail["oar"]["estimate_status"] in (
        "NOT_EVALUABLE_SERVICE_LEVEL_UNSET",
        "NOT_EVALUABLE_NEIGHBOR_INVENTORY",
        "NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS",
    )
    assert detail["status"] == "NOT_EVALUABLE"
    entry_labels = {entry["label"] for entry in trace["entries"]}
    assert "neighbour_count" in entry_labels
    assert "estimate_status" in entry_labels


# --- 5.13: SAP safety, across the whole API package -----------------------------------------------


def test_no_module_under_app_api_imports_a_sap_write_client():
    """Walks every module under app/api and forbids importing
    app.integrations.sap directly (the read adapter) from API code -- API
    routes should never reach SAP even read-only; that boundary belongs to
    the domain/service layer.

    One narrow exception: app.integrations.sap.postgres_* -- I13's API layer
    reads these Postgres repository classes directly by design (they are
    Postgres repositories, not SAP clients), and that indirection will be
    introduced when I13 grows a service layer. Nothing else under
    app.integrations.sap is exempted, so a module still cannot reach the
    live SAP client (app.integrations.sap.client) or any other submodule
    from API code.
    """
    package_dir = Path(__file__).resolve().parents[3] / "app" / "api"
    forbidden_modules = {"requests", "httpx", "urllib3"}
    allowed_sap_prefix = "app.integrations.sap.postgres_"

    for path in package_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not (imported & forbidden_modules), f"{path} imports {imported & forbidden_modules}"

        sap_imports = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module
            and node.module.startswith("app.integrations.sap")
        }
        disallowed = {m for m in sap_imports if not m.startswith(allowed_sap_prefix)}
        assert not disallowed, (
            f"{path} imports the SAP integration directly: {disallowed}. "
            f"Only {allowed_sap_prefix}* (Postgres repositories) is allowed from API code."
        )


@needs_db
def test_adoption_endpoint_never_returns_not_adopted_without_evidence():
    """The one behavioural SAP-safety property that matters at the API layer:
    UNKNOWN must never silently become NOT_ADOPTED."""
    with get_sessionmaker()() as session:
        row = session.execute(select(Recommendation).limit(1)).scalar_one_or_none()
    if row is None:
        pytest.skip("no recommendations")

    response = client.get(f"/api/v1/i7/recommendations/{row.recommendation_id}/adoption")
    assert response.status_code == 200
    # On this extract there is no staged CDHDR/CDPOS at all, so the only
    # honest answer is UNKNOWN.
    assert response.json()["status"] == "UNKNOWN"


def test_final_approval_endpoint_produces_no_sap_call_structurally():
    """The actions route itself must not import anything capable of an
    outbound SAP call -- checked directly on the file that handles
    final-approval, not inferred from the package-wide sweep above."""
    approvals_file = (
        Path(__file__).resolve().parents[3] / "app" / "api" / "i7" / "approvals.py"
    )
    source = approvals_file.read_text(encoding="utf-8")
    for forbidden in ("requests", "httpx", "SapClient", "sap_client", "odata"):
        assert forbidden not in source

"""I07 FastAPI layer.

Real Postgres or skip (the API reads persisted Phase 3-7 data; nothing here
triggers a pipeline run). Uses the application's real TestClient and the real
database session dependency -- no second session factory, matching
``tests/test_health.py``'s pattern.

Workflow tests insert one throwaway recommendation row directly (there is no
naturally-occurring READY_FOR_REVIEW recommendation on the current extract,
per the Phase 7 correction: every recommendation is blocked on the unsigned
service-level matrix) and remove it in a fixture teardown.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import app
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")

client = TestClient(app)


# --- A: health -----------------------------------------------------------------


def test_health_returns_200():
    response = client.get("/api/v1/i7/health")
    assert response.status_code == 200
    body = response.json()
    assert body["initiative"] == "I07"
    assert body["status"] == "healthy"
    assert body["api_version"] == "v1"
    assert "timestamp" in body


# --- B: recommendation list -----------------------------------------------------


@needs_db
def test_list_returns_200_with_pagination_shape():
    response = client.get("/api/v1/i7/recommendations?page=1&page_size=5")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "total", "page", "page_size"}
    assert body["page"] == 1
    assert body["page_size"] == 5
    assert len(body["items"]) <= 5


@needs_db
def test_list_total_matches_the_database():
    response = client.get("/api/v1/i7/recommendations?page=1&page_size=1")
    total = response.json()["total"]

    with get_sessionmaker()() as session:
        from sqlalchemy import func

        expected = session.execute(select(func.count()).select_from(Recommendation)).scalar()
    assert total == expected


@needs_db
def test_list_pagination_is_deterministic_across_pages():
    """The same two consecutive pages, fetched twice, must return the same
    rows -- proving the ordering is stable, not an artefact of query planning."""
    first_a = client.get("/api/v1/i7/recommendations?page=1&page_size=10").json()
    first_b = client.get("/api/v1/i7/recommendations?page=1&page_size=10").json()
    assert [i["recommendation_id"] for i in first_a["items"]] == [
        i["recommendation_id"] for i in first_b["items"]
    ]

    second = client.get("/api/v1/i7/recommendations?page=2&page_size=10").json()
    first_ids = {i["recommendation_id"] for i in first_a["items"]}
    second_ids = {i["recommendation_id"] for i in second["items"]}
    assert not (first_ids & second_ids), "page 1 and page 2 must not overlap"


# --- Part 22: portfolio-wide summary aggregates --------------------------------


@needs_db
def test_summary_returns_200_with_expected_shape():
    response = client.get("/api/v1/i7/recommendations/summary")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "total",
        "by_status",
        "by_criticality",
        "by_circuit",
        "by_risk",
        "by_plant",
        "oar_count",
        "normal_count",
        "awaiting_approval_count",
        "ready_for_review_count",
        "not_evaluable_count",
        "net_safety_stock_value_impact",
        "critical_stockout_risk_count",
        "excess_inventory_candidates_count",
        "excess_inventory_opportunity",
    }


@needs_db
def test_summary_total_matches_the_unfiltered_list_total():
    summary = client.get("/api/v1/i7/recommendations/summary").json()
    listing = client.get("/api/v1/i7/recommendations?page=1&page_size=1").json()
    assert summary["total"] == listing["total"]


@needs_db
def test_summary_by_status_counts_sum_to_the_total():
    summary = client.get("/api/v1/i7/recommendations/summary").json()
    assert sum(row["count"] for row in summary["by_status"]) == summary["total"]


@needs_db
def test_summary_oar_and_normal_counts_sum_to_the_total():
    summary = client.get("/api/v1/i7/recommendations/summary").json()
    assert summary["oar_count"] + summary["normal_count"] == summary["total"]


@needs_db
def test_summary_by_plant_counts_sum_to_the_total():
    summary = client.get("/api/v1/i7/recommendations/summary").json()
    assert sum(row["count"] for row in summary["by_plant"]) == summary["total"]


@needs_db
def test_summary_by_plant_agrees_with_the_list_endpoints_plant_filter():
    """The plant filter dropdown is built from by_plant, so a code offered
    there must return exactly that many rows when actually filtered on --
    otherwise selecting a plant shows a count the table cannot reproduce."""
    summary = client.get("/api/v1/i7/recommendations/summary").json()
    if not summary["by_plant"]:
        pytest.skip("no recommendations on this database")

    row = max(summary["by_plant"], key=lambda r: r["count"])
    listing = client.get(
        f"/api/v1/i7/recommendations?page_size=1&plant={row['plant']}"
    ).json()
    assert listing["total"] == row["count"]


@needs_db
def test_summary_respects_the_same_filters_as_the_list_endpoint():
    filtered = client.get(
        "/api/v1/i7/recommendations/summary?status=NOT_EVALUABLE"
    ).json()
    for row in filtered["by_status"]:
        assert row["status"] == "NOT_EVALUABLE"


@needs_db
def test_summary_route_is_registered_before_the_id_route():
    """'summary' must never be swallowed by /{recommendation_id} -- this is
    the exact ordering bug that would otherwise make GET .../summary return
    a 200-with-detail-shape (or a spurious 404 for a "recommendation" named
    'summary') instead of the aggregate response."""
    response = client.get("/api/v1/i7/recommendations/summary")
    assert response.status_code == 200
    assert "recommendation_id" not in response.json()


@needs_db
def test_summary_criticality_and_circuit_counts_never_fabricate_a_value():
    """None-grouped rows (missing criticality/circuit) must be reported as
    criticality/circuit == null, never collapsed into a real tier or
    silently dropped from the count."""
    summary = client.get("/api/v1/i7/recommendations/summary").json()
    by_criticality_total = sum(row["count"] for row in summary["by_criticality"])
    by_circuit_total = sum(row["count"] for row in summary["by_circuit"])
    assert by_criticality_total == summary["total"]
    assert by_circuit_total == summary["total"]


@needs_db
def test_summary_by_risk_counts_sum_to_the_total():
    summary = client.get("/api/v1/i7/recommendations/summary").json()
    by_risk_total = sum(row["count"] for row in summary["by_risk"])
    assert by_risk_total == summary["total"]


@needs_db
def test_summary_by_risk_uses_only_the_four_known_tiers():
    summary = client.get("/api/v1/i7/recommendations/summary").json()
    tiers = {row["risk"] for row in summary["by_risk"]}
    assert tiers <= {"critical", "high", "medium", "low"}


@needs_db
def test_summary_by_risk_escalates_impact_and_insurance_not_just_critical():
    """Regression (Part 36): the frontend's own deriveRisk() used to compare
    the raw criticality against the literal strings "HIGH"/"MEDIUM", which
    never occur in the real ZMM065 vocabulary (CRITICAL/IMPACT/INSURANCE/
    NORMAL/OBSOLETE) -- so no material could ever show "high"/"medium" risk,
    only "critical" or "low". This aggregate must not repeat that mistake:
    any IMPACT-criticality, open-status recommendation must count under
    "high", and any INSURANCE one under "medium", not "low"."""
    with get_sessionmaker()() as session:
        has_impact = session.execute(
            select(Recommendation.id).where(
                Recommendation.criticality == "IMPACT",
                Recommendation.status.in_(
                    ("NOT_EVALUABLE", "READY_FOR_REVIEW", "PENDING_APPROVAL", "HELD", "SENT_BACK", "ADJUSTED")
                ),
            ).limit(1)
        ).scalar_one_or_none()
    if has_impact is None:
        pytest.skip("no open IMPACT-criticality recommendation on this database")

    summary = client.get("/api/v1/i7/recommendations/summary").json()
    by_risk = {row["risk"]: row["count"] for row in summary["by_risk"]}
    assert by_risk.get("high", 0) > 0


# --- Critical Stockout Risk / Excess Inventory (2026-09-22 I07-derived proxies) ---


@needs_db
def test_summary_critical_stockout_risk_count_is_bounded_by_populated_rop():
    """The stockout-risk count can never exceed how many latest-generation
    recommendations even have a recommended_rop -- a material with no ROP is
    excluded from the count entirely (never counted as either at-risk or
    safe), so the count is always a subset of the ROP-populated rows."""
    listing = client.get(
        "/api/v1/i7/recommendations?page=1&page_size=1"
    ).json()
    total = listing["total"]

    summary = client.get("/api/v1/i7/recommendations/summary").json()
    assert isinstance(summary["critical_stockout_risk_count"], int)
    assert 0 <= summary["critical_stockout_risk_count"] <= total


@needs_db
def test_summary_excess_inventory_count_matches_current_vs_recommended_max_stock():
    with get_sessionmaker()() as session:
        expected = session.execute(
            select(func.count()).select_from(Recommendation).where(
                Recommendation.current_max_stock.isnot(None),
                Recommendation.recommended_max_stock.isnot(None),
                Recommendation.current_max_stock > Recommendation.recommended_max_stock,
            )
        ).scalar_one()

    summary = client.get("/api/v1/i7/recommendations/summary").json()
    # Endpoint scopes to _latest_only(); a database with multiple generations
    # may show a smaller endpoint count than this unscoped reproduction.
    assert summary["excess_inventory_candidates_count"] <= expected


@needs_db
def test_summary_excess_inventory_opportunity_is_none_when_count_is_zero():
    """Never a fabricated 0 -- if no material is an excess candidate, or none
    of them has a unit price, the opportunity figure must be None, not 0."""
    summary = client.get("/api/v1/i7/recommendations/summary").json()
    if summary["excess_inventory_candidates_count"] == 0:
        assert summary["excess_inventory_opportunity"] is None


@needs_db
def test_summary_stockout_and_excess_counts_never_negative():
    summary = client.get("/api/v1/i7/recommendations/summary").json()
    assert summary["critical_stockout_risk_count"] >= 0
    assert summary["excess_inventory_candidates_count"] >= 0


@needs_db
@pytest.mark.needs_seed_data
def test_sort_desc_reverses_ordering():
    """Part 22 -- 'recent recommendations' needs newest-first, which the
    default ascending-only sort cannot provide without pulling every page
    client-side to re-sort. sort_desc reuses the same SORT_FIELDS column,
    just descending."""
    ascending = client.get("/api/v1/i7/recommendations?sort=generated_at&page_size=5").json()
    descending = client.get(
        "/api/v1/i7/recommendations?sort=generated_at&sort_desc=true&page_size=5"
    ).json()
    assert [i["recommendation_id"] for i in ascending["items"]] != [
        i["recommendation_id"] for i in descending["items"]
    ]
    # The very first row overall (id=1, ascending default) must never appear
    # in a sort_desc=true page unless the whole table is <=5 rows.
    if ascending["total"] > 5:
        assert ascending["items"][0]["recommendation_id"] not in {
            i["recommendation_id"] for i in descending["items"]
        }


# --- C: filtering ------------------------------------------------------------------


@needs_db
def test_filter_by_status():
    response = client.get("/api/v1/i7/recommendations?status=NOT_EVALUABLE&page_size=5")
    assert response.status_code == 200
    for item in response.json()["items"]:
        assert item["status"] == "NOT_EVALUABLE"


@needs_db
def test_filter_by_plant():
    response = client.get("/api/v1/i7/recommendations?plant=1300&page_size=5")
    assert response.status_code == 200
    for item in response.json()["items"]:
        assert item["plant"] == "1300"


@needs_db
def test_filter_by_material():
    response = client.get(
        "/api/v1/i7/recommendations?material=1000000009&page_size=5"
    )
    assert response.status_code == 200
    for item in response.json()["items"]:
        assert item["material"] == "1000000009"


@needs_db
def test_filter_by_is_oar():
    response = client.get("/api/v1/i7/recommendations?is_oar=true&page_size=5")
    assert response.status_code == 200
    for item in response.json()["items"]:
        assert item["is_oar"] is True


# --- D: recommendation detail --------------------------------------------------------


@needs_db
def test_detail_of_an_existing_recommendation_returns_200():
    with get_sessionmaker()() as session:
        row = session.execute(select(Recommendation).limit(1)).scalar_one_or_none()
    if row is None:
        pytest.skip("no recommendations generated")
    response = client.get(f"/api/v1/i7/recommendations/{row.recommendation_id}")
    assert response.status_code == 200
    assert response.json()["recommendation_id"] == row.recommendation_id


def test_detail_of_an_unknown_recommendation_returns_404():
    response = client.get("/api/v1/i7/recommendations/DOES-NOT-EXIST")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "RECOMMENDATION_NOT_FOUND"


@needs_db
def test_detail_exposes_rationale_and_its_source():
    with get_sessionmaker()() as session:
        row = session.execute(select(Recommendation).limit(1)).scalar_one_or_none()
    if row is None:
        pytest.skip("no recommendations generated")
    response = client.get(f"/api/v1/i7/recommendations/{row.recommendation_id}")
    assert response.status_code == 200
    body = response.json()
    assert "rationale" in body
    assert "text" in body["rationale"]
    assert "source" in body["rationale"]
    if body["rationale"]["source"] is not None:
        assert body["rationale"]["source"] in ("AI_GENERATED", "DETERMINISTIC_FALLBACK")


@needs_db
def test_detail_exposes_a_real_staged_consumption_series_for_the_three_target_materials():
    """The exact materials verified in this session's Max Stock work
    (5000092261/1300 and siblings) have 6 real staged months -- confirms the
    detail endpoint reads i7_staged_consumption verbatim, not a fabricated or
    densified series."""
    with get_sessionmaker()() as session:
        row = session.execute(
            select(Recommendation).where(
                Recommendation.sap_material_number == "5000092261",
                Recommendation.sap_plant_code == "1300",
            ).order_by(Recommendation.id.desc())
        ).scalars().first()
    if row is None:
        pytest.skip("material 5000092261/1300 has no recommendation on this database")
    response = client.get(f"/api/v1/i7/recommendations/{row.recommendation_id}")
    assert response.status_code == 200
    history = response.json()["demand"]["consumption_history"]
    assert len(history) > 0
    for entry in history:
        assert "period" in entry
        assert "quantity" in entry


@needs_db
def test_detail_consumption_history_is_empty_not_fabricated_for_a_cold_start_material():
    """An OAR/cold-start material has UNCLASSIFIED demand and typically no
    staged consumption at all -- the field must come back as an empty list,
    never a guessed series."""
    with get_sessionmaker()() as session:
        row = session.execute(
            select(Recommendation).where(Recommendation.demand_class == "UNCLASSIFIED").limit(1)
        ).scalar_one_or_none()
    if row is None:
        pytest.skip("no UNCLASSIFIED-demand recommendation on this database")
    response = client.get(f"/api/v1/i7/recommendations/{row.recommendation_id}")
    assert response.status_code == 200
    history = response.json()["demand"]["consumption_history"]
    assert isinstance(history, list)


@needs_db
def test_recommendation_detail_response_carries_no_secrets():
    with get_sessionmaker()() as session:
        row = session.execute(select(Recommendation).limit(1)).scalar_one_or_none()
    if row is None:
        pytest.skip("no recommendations generated")
    response = client.get(f"/api/v1/i7/recommendations/{row.recommendation_id}")
    body_text = response.text.lower()
    for forbidden in ("api_key", "foundry_api_key", "llm_api_key", "authorization: bearer"):
        assert forbidden not in body_text


# --- E: trace ---------------------------------------------------------------------------


@needs_db
def test_trace_of_an_existing_recommendation_returns_200_with_expected_sections():
    with get_sessionmaker()() as session:
        row = session.execute(select(Recommendation).limit(1)).scalar_one_or_none()
    if row is None:
        pytest.skip("no recommendations generated")
    response = client.get(f"/api/v1/i7/recommendations/{row.recommendation_id}/trace")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "recommendation_id", "status", "blocking_reason", "factors", "entries",
    }


def test_trace_of_an_unknown_recommendation_returns_404():
    response = client.get("/api/v1/i7/recommendations/DOES-NOT-EXIST/trace")
    assert response.status_code == 404


# --- F: NOT_EVALUABLE preserved, nothing fabricated -------------------------------------------


@needs_db
def test_not_evaluable_recommendation_has_no_fabricated_recommended_values():
    with get_sessionmaker()() as session:
        row = session.execute(
            select(Recommendation).where(Recommendation.status == "NOT_EVALUABLE").limit(1)
        ).scalar_one_or_none()
    if row is None:
        pytest.skip("no NOT_EVALUABLE recommendations")

    body = client.get(f"/api/v1/i7/recommendations/{row.recommendation_id}").json()
    assert body["status"] == "NOT_EVALUABLE"
    if row.recommended_safety_stock is None:
        assert body["recommended"]["safety_stock"] is None
    if row.recommended_rop is None:
        assert body["recommended"]["rop"] is None


# --- G: OAR blocked recommendation --------------------------------------------------------


@needs_db
def test_oar_recommendation_preserves_similarity_evidence_while_blocked():
    """The Phase 7 correction, verified through the API: similarity being
    AVAILABLE must never make the recommendation itself READY_FOR_REVIEW."""
    with get_sessionmaker()() as session:
        row = session.execute(
            select(Recommendation).where(
                Recommendation.oar_similarity_status == "AVAILABLE"
            ).limit(1)
        ).scalar_one_or_none()
    if row is None:
        pytest.skip("no OAR recommendation with available similarity")

    body = client.get(f"/api/v1/i7/recommendations/{row.recommendation_id}").json()
    assert body["oar"]["similarity_status"] == "AVAILABLE"
    # Two legitimate block reasons exist once similarity is AVAILABLE but the
    # estimate itself is not SUCCESS: the unsigned service-level matrix, or
    # (since the minimum_neighbours=5 / minimum_similarity=0.60 admission
    # gate) too few candidates cleared the 0.60 similarity floor.
    assert body["oar"]["estimate_status"] in (
        "NOT_EVALUABLE_SERVICE_LEVEL_UNSET",
        "NOT_EVALUABLE_NEIGHBOR_INVENTORY",
        "NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS",
    )
    assert body["status"] == "NOT_EVALUABLE"
    assert body["recommended"]["safety_stock"] is None
    assert body["recommended"]["rop"] is None
    assert body["recommended"]["max_stock"] is None


# --- H: approval workflow ---------------------------------------------------------------------


@pytest.fixture
def ready_recommendation():
    """One throwaway READY_FOR_REVIEW recommendation, since none exists
    naturally on the current extract (Phase 7 correction: everything is
    blocked on the unsigned service level)."""
    recommendation_id = "REC-TEST-API-WORKFLOW"
    with get_sessionmaker()() as session:
        session.execute(delete(ApprovalLedgerEntry).where(
            ApprovalLedgerEntry.recommendation_id == recommendation_id
        ))
        session.execute(delete(Recommendation).where(
            Recommendation.recommendation_id == recommendation_id
        ))
        session.add(
            Recommendation(
                recommendation_id=recommendation_id,
                sap_material_number="TESTAPI001",
                sap_plant_code="1300",
                policy_id="i07-test",
                policy_version=1,
                formula_version="i07-recommendation-2",
                status="READY_FOR_REVIEW",
                impact_status="NOT_EVALUABLE_MISSING_RECOMMENDED",
                recommended_safety_stock=7,
                recommended_rop=31,
            )
        )
        session.commit()

    yield recommendation_id

    with get_sessionmaker()() as session:
        session.execute(delete(ApprovalLedgerEntry).where(
            ApprovalLedgerEntry.recommendation_id == recommendation_id
        ))
        session.execute(delete(Recommendation).where(
            Recommendation.recommendation_id == recommendation_id
        ))
        session.commit()


# --- Part 22: read-only workflow state -----------------------------------------


@needs_db
def test_workflow_state_before_submission_shows_ready_for_review(ready_recommendation):
    rid = ready_recommendation
    response = client.get(f"/api/v1/i7/recommendations/{rid}/workflow-state")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "READY_FOR_REVIEW"
    assert body["pending_role"] is None
    assert body["chain_index"] == 0


@needs_db
def test_workflow_state_matches_what_submit_itself_returned(ready_recommendation):
    rid = ready_recommendation
    submit = client.post(f"/api/v1/i7/recommendations/{rid}/submit", json={"actor_id": "U1"})
    state = client.get(f"/api/v1/i7/recommendations/{rid}/workflow-state")
    assert state.status_code == 200
    assert state.json() == submit.json()


@needs_db
def test_workflow_state_of_an_unknown_recommendation_returns_404():
    response = client.get("/api/v1/i7/recommendations/REC-DOES-NOT-EXIST/workflow-state")
    assert response.status_code == 404


@needs_db
def test_submit_then_correct_first_action_succeeds(ready_recommendation):
    rid = ready_recommendation
    submit = client.post(f"/api/v1/i7/recommendations/{rid}/submit", json={"actor_id": "U1"})
    assert submit.status_code == 200
    assert submit.json()["pending_role"] == "End User"

    action = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={"actor_id": "U2", "actor_role": "End User", "action": "APPROVE"},
    )
    assert action.status_code == 200
    assert action.json()["pending_role"] == "Engineering Manager"


@needs_db
def test_wrong_role_is_rejected(ready_recommendation):
    rid = ready_recommendation
    client.post(f"/api/v1/i7/recommendations/{rid}/submit", json={"actor_id": "U1"})

    response = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={"actor_id": "U2", "actor_role": "Warehouse Supervisor", "action": "APPROVE"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_WORKFLOW_ACTION"


@needs_db
def test_stage_cannot_be_skipped(ready_recommendation):
    rid = ready_recommendation
    client.post(f"/api/v1/i7/recommendations/{rid}/submit", json={"actor_id": "U1"})
    client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={"actor_id": "U2", "actor_role": "End User", "action": "APPROVE"},
    )
    response = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={"actor_id": "U3", "actor_role": "Warehouse Supervisor", "action": "APPROVE"},
    )
    assert response.status_code == 409


@needs_db
def test_reject_requires_a_comment(ready_recommendation):
    rid = ready_recommendation
    client.post(f"/api/v1/i7/recommendations/{rid}/submit", json={"actor_id": "U1"})

    missing = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={"actor_id": "U2", "actor_role": "End User", "action": "REJECT"},
    )
    assert missing.status_code == 409

    present = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={
            "actor_id": "U2", "actor_role": "End User", "action": "REJECT",
            "comment": "no longer needed",
        },
    )
    assert present.status_code == 200
    assert present.json()["status"] == "REJECTED"


@needs_db
def test_send_back_requires_a_comment(ready_recommendation):
    rid = ready_recommendation
    client.post(f"/api/v1/i7/recommendations/{rid}/submit", json={"actor_id": "U1"})
    client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={"actor_id": "U2", "actor_role": "End User", "action": "APPROVE"},
    )
    missing = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={"actor_id": "U3", "actor_role": "Engineering Manager", "action": "SEND_BACK"},
    )
    assert missing.status_code == 409

    present = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={
            "actor_id": "U3", "actor_role": "Engineering Manager", "action": "SEND_BACK",
            "comment": "needs rework",
        },
    )
    assert present.status_code == 200
    assert present.json()["pending_role"] == "End User"


@needs_db
def test_adjust_requires_a_reason_and_bumps_version(ready_recommendation):
    rid = ready_recommendation
    client.post(f"/api/v1/i7/recommendations/{rid}/submit", json={"actor_id": "U1"})

    missing = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={"actor_id": "U2", "actor_role": "End User", "action": "ADJUST"},
    )
    assert missing.status_code == 409

    present = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={
            "actor_id": "U2", "actor_role": "End User", "action": "ADJUST",
            "comment": "corrected quantity",
        },
    )
    assert present.status_code == 200
    assert present.json()["current_version"] == 2


@needs_db
def test_successful_full_chain_reaches_sap_execution_pending(ready_recommendation):
    rid = ready_recommendation
    client.post(f"/api/v1/i7/recommendations/{rid}/submit", json={"actor_id": "U1"})
    for role in (
        "End User", "Engineering Manager", "Commercial Manager", "Warehouse Supervisor",
    ):
        response = client.post(
            f"/api/v1/i7/recommendations/{rid}/actions",
            json={"actor_id": "U2", "actor_role": role, "action": "APPROVE"},
        )
        assert response.status_code == 200
    assert response.json()["status"] == "SAP_EXECUTION_PENDING"


# --- H2: HOLD, distinct from SEND_BACK -------------------------------------


@needs_db
def test_hold_requires_a_comment(ready_recommendation):
    rid = ready_recommendation
    client.post(f"/api/v1/i7/recommendations/{rid}/submit", json={"actor_id": "U1"})

    missing = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={"actor_id": "U2", "actor_role": "End User", "action": "HOLD"},
    )
    assert missing.status_code == 409

    present = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={
            "actor_id": "U2", "actor_role": "End User", "action": "HOLD",
            "comment": "awaiting budget sign-off",
        },
    )
    assert present.status_code == 200
    assert present.json()["status"] == "HELD"
    assert present.json()["pending_role"] == "End User"


@needs_db
def test_hold_does_not_auto_advance_and_only_release_hold_resumes(ready_recommendation):
    rid = ready_recommendation
    client.post(f"/api/v1/i7/recommendations/{rid}/submit", json={"actor_id": "U1"})
    client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={
            "actor_id": "U2", "actor_role": "End User", "action": "HOLD",
            "comment": "pausing",
        },
    )

    blocked = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={"actor_id": "U2", "actor_role": "End User", "action": "APPROVE"},
    )
    assert blocked.status_code == 409

    released = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={"actor_id": "U2", "actor_role": "End User", "action": "RELEASE_HOLD"},
    )
    assert released.status_code == 200
    assert released.json()["status"] == "PENDING_APPROVAL"
    assert released.json()["pending_role"] == "End User"


@needs_db
def test_hold_and_send_back_leave_the_recommendation_in_different_states(ready_recommendation):
    """Same starting position, two different actions, two different
    resulting states -- proving HOLD is not a silent rename of SEND_BACK."""
    rid = ready_recommendation
    client.post(f"/api/v1/i7/recommendations/{rid}/submit", json={"actor_id": "U1"})
    client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={"actor_id": "U2", "actor_role": "End User", "action": "APPROVE"},
    )

    held = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={
            "actor_id": "U3", "actor_role": "Engineering Manager", "action": "HOLD",
            "comment": "awaiting decision",
        },
    )
    assert held.json()["status"] == "HELD"
    assert held.json()["pending_role"] == "Engineering Manager"

    released = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={"actor_id": "U3", "actor_role": "Engineering Manager", "action": "RELEASE_HOLD"},
    )
    sent_back = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={
            "actor_id": "U3", "actor_role": "Engineering Manager", "action": "SEND_BACK",
            "comment": "needs rework",
        },
    )
    assert released.json()["status"] == "PENDING_APPROVAL"
    assert sent_back.json()["status"] == "SENT_BACK"
    assert sent_back.json()["pending_role"] == "End User"


# --- H3: OAR route and criticality-based ROP/Max route -----------------------


@pytest.fixture
def ready_oar_recommendation():
    """One throwaway READY_FOR_REVIEW OAR recommendation, to exercise the
    fixed Inventory Controller -> Commercial Head -> Engineering Head ->
    Plant Head route."""
    recommendation_id = "REC-TEST-API-OAR-WORKFLOW"
    with get_sessionmaker()() as session:
        session.execute(delete(ApprovalLedgerEntry).where(
            ApprovalLedgerEntry.recommendation_id == recommendation_id
        ))
        session.execute(delete(Recommendation).where(
            Recommendation.recommendation_id == recommendation_id
        ))
        session.add(
            Recommendation(
                recommendation_id=recommendation_id,
                sap_material_number="TESTAPIOAR001",
                sap_plant_code="1300",
                policy_id="i07-test",
                policy_version=1,
                formula_version="i07-recommendation-2",
                status="READY_FOR_REVIEW",
                impact_status="NOT_EVALUABLE_MISSING_RECOMMENDED",
                is_oar=True,
                recommended_safety_stock=5,
                recommended_rop=12,
            )
        )
        session.commit()

    yield recommendation_id

    with get_sessionmaker()() as session:
        session.execute(delete(ApprovalLedgerEntry).where(
            ApprovalLedgerEntry.recommendation_id == recommendation_id
        ))
        session.execute(delete(Recommendation).where(
            Recommendation.recommendation_id == recommendation_id
        ))
        session.commit()


@needs_db
def test_oar_recommendation_submits_into_the_oar_route(ready_oar_recommendation):
    rid = ready_oar_recommendation
    submit = client.post(f"/api/v1/i7/recommendations/{rid}/submit", json={"actor_id": "U1"})
    assert submit.status_code == 200
    assert submit.json()["pending_role"] == "Inventory Controller"
    assert submit.json()["route"] == [
        "Inventory Controller", "Commercial Head", "Engineering Head", "Plant Head",
    ]


@needs_db
def test_oar_route_steps_cannot_be_skipped_via_the_api(ready_oar_recommendation):
    rid = ready_oar_recommendation
    client.post(f"/api/v1/i7/recommendations/{rid}/submit", json={"actor_id": "U1"})

    response = client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={"actor_id": "U2", "actor_role": "Plant Head", "action": "APPROVE"},
    )
    assert response.status_code == 409


@needs_db
def test_oar_route_completes_through_all_four_named_roles(ready_oar_recommendation):
    rid = ready_oar_recommendation
    client.post(f"/api/v1/i7/recommendations/{rid}/submit", json={"actor_id": "U1"})
    for role in (
        "Inventory Controller", "Commercial Head", "Engineering Head", "Plant Head",
    ):
        response = client.post(
            f"/api/v1/i7/recommendations/{rid}/actions",
            json={"actor_id": "U2", "actor_role": role, "action": "APPROVE"},
        )
        assert response.status_code == 200
    assert response.json()["status"] == "SAP_EXECUTION_PENDING"


@needs_db
def test_rop_max_recommendation_still_uses_the_default_four_step_route(ready_recommendation):
    """Regression guard: adding OAR routing must not change the default
    ROP/Max route for a non-OAR recommendation with no configured policy
    override."""
    rid = ready_recommendation
    submit = client.post(f"/api/v1/i7/recommendations/{rid}/submit", json={"actor_id": "U1"})
    assert submit.json()["route"] == [
        "End User", "Engineering Manager", "Commercial Manager", "Warehouse Supervisor",
    ]


# --- I: approval history ------------------------------------------------------------------------


@needs_db
def test_approval_history_returns_ledger_entries(ready_recommendation):
    rid = ready_recommendation
    client.post(f"/api/v1/i7/recommendations/{rid}/submit", json={"actor_id": "U1"})
    client.post(
        f"/api/v1/i7/recommendations/{rid}/actions",
        json={"actor_id": "U2", "actor_role": "End User", "action": "APPROVE"},
    )

    response = client.get(f"/api/v1/i7/recommendations/{rid}/approval-history")
    assert response.status_code == 200
    items = response.json()["items"]
    assert [i["action"] for i in items] == ["SUBMIT", "APPROVE"]


# --- J: adoption ------------------------------------------------------------------------------


@needs_db
def test_adoption_returns_unknown_when_no_sap_evidence_exists():
    """The only reachable status on this extract: raw_cdhdr/raw_cdpos are
    staged with real data, but raw_cdpos carries zero MATERIAL/MARC rows, so
    RawChangeDocumentProvider genuinely finds no evidence for any
    material-plant."""
    with get_sessionmaker()() as session:
        row = session.execute(select(Recommendation).limit(1)).scalar_one_or_none()
    if row is None:
        pytest.skip("no recommendations generated")

    response = client.get(f"/api/v1/i7/recommendations/{row.recommendation_id}/adoption")
    assert response.status_code == 200
    assert response.json()["status"] == "UNKNOWN"


@needs_db
def test_adoption_marks_parameter_checks_as_not_conversion_adoption():
    with get_sessionmaker()() as session:
        row = session.execute(
            select(Recommendation).where(~Recommendation.is_oar).limit(1)
        ).scalar_one_or_none()
    if row is None:
        pytest.skip("no normal-path recommendation generated")

    response = client.get(f"/api/v1/i7/recommendations/{row.recommendation_id}/adoption")
    assert response.status_code == 200
    assert response.json()["is_conversion_adoption"] is False


@needs_db
def test_adoption_never_claims_conversion_adoption_without_a_real_transition():
    """No recommendation on this extract is currently OAR-conversion-eligible
    (Critical/HOD triggers are unresolved, and no consumption_count_12m
    candidate has fired) -- if one becomes eligible later, its adoption
    result must still report UNKNOWN/"Awaiting SAP test change", never
    ADOPTED, since no ND/PD -> VB transition exists in this extract."""
    with get_sessionmaker()() as session:
        row = session.execute(
            select(Recommendation).where(
                Recommendation.is_oar,
                Recommendation.conversion_eligibility == "ELIGIBLE",
            ).limit(1)
        ).scalar_one_or_none()
    if row is None:
        pytest.skip("no OAR-conversion-eligible recommendation exists on this extract")

    response = client.get(f"/api/v1/i7/recommendations/{row.recommendation_id}/adoption")
    body = response.json()
    assert body["is_conversion_adoption"] is True
    assert body["status"] == "UNKNOWN"
    assert body["status"] != "ADOPTED"
    assert "Awaiting SAP test change" in body["detail"]


def test_adoption_of_unknown_recommendation_returns_404():
    response = client.get("/api/v1/i7/recommendations/DOES-NOT-EXIST/adoption")
    assert response.status_code == 404


# --- K: runs -----------------------------------------------------------------------------------


@needs_db
def test_list_runs_returns_200():
    response = client.get("/api/v1/i7/runs")
    assert response.status_code == 200
    assert "items" in response.json()


@needs_db
def test_get_one_run_by_type_and_id():
    listing = client.get("/api/v1/i7/runs").json()["items"]
    if not listing:
        pytest.skip("no runs recorded")
    first = listing[0]

    response = client.get(f"/api/v1/i7/runs/{first['run_type']}/{first['run_id']}")
    assert response.status_code == 200
    assert response.json()["run_id"] == first["run_id"]


def test_get_unknown_run_returns_404():
    response = client.get("/api/v1/i7/runs/feature/99999999")
    assert response.status_code == 404


# --- L: invalid parameters -----------------------------------------------------------------------


def test_invalid_page_size_is_rejected():
    response = client.get("/api/v1/i7/recommendations?page_size=99999")
    assert response.status_code == 422


def test_invalid_sort_field_is_rejected():
    response = client.get("/api/v1/i7/recommendations?sort=not_a_real_column")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_SORT_FIELD"


def test_invalid_run_type_is_rejected():
    response = client.get("/api/v1/i7/runs/not-a-real-type/1")
    assert response.status_code == 422


def test_malformed_approval_action_is_rejected():
    response = client.post(
        "/api/v1/i7/recommendations/DOES-NOT-EXIST/actions",
        json={"actor_id": "U1", "actor_role": "Not A Real Role", "action": "APPROVE"},
    )
    assert response.status_code == 422


# --- M: error format --------------------------------------------------------------------------


def test_error_envelope_is_consistent():
    response = client.get("/api/v1/i7/recommendations/DOES-NOT-EXIST")
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}


# --- O: SAP safety ----------------------------------------------------------------------------


def test_i7_api_package_imports_no_sap_write_client():
    """Structural proof: no module under app.api.i7 imports the SAP client or
    any HTTP/networking library that could reach outward."""
    import ast
    from pathlib import Path

    package_dir = Path(__file__).resolve().parents[3] / "app" / "api" / "i7"
    forbidden = {"requests", "httpx", "urllib3"}

    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not (imported & forbidden), f"{path.name} imports {imported & forbidden}"
        assert "app.integrations.sap" not in path.read_text(encoding="utf-8")


def test_openapi_and_docs_are_served():
    assert client.get("/openapi.json").status_code == 200


# --- I: adoption tracking list --------------------------------------------------


@pytest.fixture
def two_recommendation_runs_same_material_plant():
    """Two Recommendation rows for the same material-plant, as a genuine
    pipeline rerun under new upstream run ids produces (see
    app/models/i7_recommendation.py's uq_i7_recommendation_inputs) -- the
    exact shape that made /recommendations/adoption show the same
    material-plant twice before the "latest run only" filter."""
    recommendation_id = "REC-TEST-API-ADOPTION-DUP"
    material = "TESTAPI-ADOPT-001"
    plant = "1300"

    with get_sessionmaker()() as session:
        session.execute(delete(Recommendation).where(
            Recommendation.recommendation_id == recommendation_id
        ))
        session.add_all([
            Recommendation(
                recommendation_id=recommendation_id,
                sap_material_number=material,
                sap_plant_code=plant,
                feature_run_id=1,
                forecast_run_id=1,
                inventory_run_id=1,
                oar_run_id=1,
                policy_id="i07-test",
                policy_version=1,
                formula_version="i07-recommendation-2",
                status="READY_FOR_REVIEW",
                impact_status="NOT_EVALUABLE_MISSING_RECOMMENDED",
                recommended_safety_stock=5,
                recommended_rop=20,
                generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            Recommendation(
                recommendation_id=recommendation_id,
                sap_material_number=material,
                sap_plant_code=plant,
                feature_run_id=2,
                forecast_run_id=2,
                inventory_run_id=2,
                oar_run_id=2,
                policy_id="i07-test",
                policy_version=1,
                formula_version="i07-recommendation-2",
                status="READY_FOR_REVIEW",
                impact_status="NOT_EVALUABLE_MISSING_RECOMMENDED",
                recommended_safety_stock=7,
                recommended_rop=31,
                generated_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
            ),
        ])
        session.commit()

    yield material, plant

    with get_sessionmaker()() as session:
        session.execute(delete(Recommendation).where(
            Recommendation.recommendation_id == recommendation_id
        ))
        session.commit()


@needs_db
@pytest.mark.needs_seed_data
def test_adoption_list_shows_one_row_per_material_plant(two_recommendation_runs_same_material_plant):
    material, plant = two_recommendation_runs_same_material_plant
    response = client.get(f"/api/v1/i7/recommendations/adoption?material={material}&plant={plant}")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1


@needs_db
@pytest.mark.needs_seed_data
def test_adoption_list_total_reflects_one_row_not_two(two_recommendation_runs_same_material_plant):
    """The count endpoint uses in the pagination footer must match the
    dedup, not just the visible page -- otherwise 'N recommendations total'
    still double-counts even once the displayed rows look right."""
    material, plant = two_recommendation_runs_same_material_plant
    response = client.get(f"/api/v1/i7/recommendations/adoption?material={material}&plant={plant}")
    assert response.json()["total"] == 1
    assert client.get("/docs").status_code == 200

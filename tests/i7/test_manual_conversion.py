"""Manual Min-Max conversion of a NOT_ELIGIBLE OAR material, through the existing submit and approval endpoints.

In-memory SQLite with the session dependency overridden (the shape tests/i7/api/test_approval_events.py uses), so the
whole path -- submit, approve, reject, resubmit, the ledger, the list response -- runs without the Azure SQL server.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.i7.deps import get_session
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import routing
from app.main import create_app
from app.models.i7_features import DecliningDemandEvaluation, MaterialFeature
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation
from app.models.i7_staging import StagedMaterial, StagedMaterialPlant

BASE = "/api/v1/i7"
TABLES = (Recommendation, ApprovalLedgerEntry, StagedMaterial, StagedMaterialPlant, MaterialFeature, DecliningDemandEvaluation)
OAR_ROUTE = [role for role in routing.route_for(True, None, PolicyDocument())]

VERDICT_DETAIL = "It was used 0 times in the last 6 months, which does not exceed the 4 needed."


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in TABLES:
        model.__table__.to_metadata(metadata).indexes.clear()
    metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def client(factory):
    app = create_app()

    def _session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    # No `with`: entering the context runs the lifespan against the real database.
    yield TestClient(app)
    app.dependency_overrides.clear()


def seed(factory, recommendation_id="REC-OAR-1", *, verdict="NOT_ELIGIBLE", status="READY_FOR_REVIEW", is_oar=True, **overrides):
    values = dict(
        recommendation_id=recommendation_id, sap_material_number="5000080431", sap_plant_code="1500",
        feature_run_id=1, forecast_run_id=1, inventory_run_id=1, oar_run_id=1, policy_id="p", policy_version=1,
        formula_version="f", status=status, impact_status="AVAILABLE", is_oar=is_oar,
        conversion_eligibility=verdict, conversion_trigger="NONE", conversion_detail=VERDICT_DETAIL,
        unit_price=Decimal("16363"), current_rop=Decimal("0"), recommended_rop=Decimal("2"),
        current_safety_stock=Decimal("0"), recommended_safety_stock=Decimal("1"),
        current_max_stock=Decimal("0"), recommended_max_stock=Decimal("4"),
        generated_at=datetime(2026, 10, 14, tzinfo=timezone.utc), updated_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
        chain_index=0, adjustment_count=0, current_version=1,
    )
    values.update(overrides)
    with factory() as session:
        session.add(Recommendation(**values))
        session.commit()


def row(factory, recommendation_id="REC-OAR-1") -> Recommendation:
    with factory() as session:
        found = session.execute(select(Recommendation).where(Recommendation.recommendation_id == recommendation_id)).scalar_one()
        session.expunge(found)
        return found


def ledger(factory, recommendation_id="REC-OAR-1") -> list[ApprovalLedgerEntry]:
    with factory() as session:
        return list(
            session.execute(
                select(ApprovalLedgerEntry).where(ApprovalLedgerEntry.recommendation_id == recommendation_id).order_by(ApprovalLedgerEntry.id)
            ).scalars()
        )


def submit(client, recommendation_id="REC-OAR-1", actor="planner-1"):
    return client.post(f"{BASE}/recommendations/{recommendation_id}/submit", json={"actor_id": actor})


def act(client, action, role, comment=None, recommendation_id="REC-OAR-1"):
    return client.post(
        f"{BASE}/recommendations/{recommendation_id}/actions",
        json={"actor_id": "approver-1", "actor_role": role.value, "action": action, "comment": comment},
    )


# --- submission records the manual request -------------------------------------------------------


def test_submitting_a_not_eligible_oar_material_records_a_manual_conversion_request(client, factory):
    seed(factory)

    response = submit(client, actor="planner-1")

    assert response.status_code == 200
    assert response.json()["status"] == "PENDING_APPROVAL"
    saved = row(factory)
    assert saved.manually_converted is True
    assert saved.requested_by == "planner-1"
    assert saved.requested_at is not None


def test_the_rule_based_verdict_and_its_reason_are_not_changed_by_the_request(client, factory):
    seed(factory)

    submit(client)

    saved = row(factory)
    assert saved.conversion_eligibility == "NOT_ELIGIBLE"
    assert saved.conversion_trigger == "NONE"
    assert saved.conversion_detail == VERDICT_DETAIL


def test_the_request_goes_into_the_existing_oar_chain_at_its_first_role(client, factory):
    seed(factory)

    body = submit(client).json()

    assert body["route"] == [role.value for role in OAR_ROUTE]
    assert body["pending_role"] == OAR_ROUTE[0].value
    assert [entry.action for entry in ledger(factory)] == ["SUBMIT"]
    assert ledger(factory)[0].actor_id == "planner-1"


def test_an_eligible_material_is_not_a_manual_conversion(client, factory):
    seed(factory, verdict="ELIGIBLE")

    submit(client)

    saved = row(factory)
    assert saved.manually_converted is False
    assert saved.requested_by is None and saved.requested_at is None
    assert saved.status == "PENDING_APPROVAL"


def test_a_stock_item_with_no_conversion_verdict_is_not_a_manual_conversion(client, factory):
    seed(factory, verdict=None, is_oar=False)

    submit(client)

    assert row(factory).manually_converted is False


def test_a_request_without_all_three_recommended_values_is_refused_and_writes_nothing(client, factory):
    seed(factory, recommended_max_stock=None)

    response = submit(client)

    assert response.status_code == 409
    saved = row(factory)
    assert saved.status == "READY_FOR_REVIEW" and saved.manually_converted is False and saved.requested_by is None
    assert ledger(factory) == []


def test_a_not_evaluable_material_cannot_be_requested(client, factory):
    seed(factory, status="NOT_EVALUABLE", recommended_rop=None, recommended_safety_stock=None, recommended_max_stock=None)

    response = submit(client)

    assert response.status_code == 409
    assert row(factory).manually_converted is False


# --- duplicates ----------------------------------------------------------------------------------


def test_a_second_request_while_the_first_is_pending_is_refused(client, factory):
    seed(factory)
    submit(client, actor="planner-1")

    again = submit(client, actor="planner-2")

    assert again.status_code == 409
    assert [entry.action for entry in ledger(factory)] == ["SUBMIT"]
    assert row(factory).requested_by == "planner-1"  # the first request stands


@pytest.mark.parametrize("action,comment", [("HOLD", "waiting"), ("SEND_BACK", "rework")])
def test_a_held_or_sent_back_request_cannot_be_requested_again(client, factory, action, comment):
    seed(factory)
    submit(client)
    act(client, action, OAR_ROUTE[0], comment)

    assert submit(client).status_code == 409


# --- approval and rejection ----------------------------------------------------------------------


def test_an_approved_request_continues_to_sap_execution_without_another_submit(client, factory):
    seed(factory)
    submit(client)

    for role in OAR_ROUTE:
        assert act(client, "APPROVE", role).status_code == 200

    saved = row(factory)
    assert saved.status == "SAP_EXECUTION_PENDING"
    assert saved.manually_converted is True and saved.conversion_eligibility == "NOT_ELIGIBLE"
    assert [entry.action for entry in ledger(factory)] == ["SUBMIT"] + ["APPROVE"] * len(OAR_ROUTE)


def test_a_rejection_needs_a_reason_and_ends_the_request(client, factory):
    seed(factory)
    submit(client)

    assert act(client, "REJECT", OAR_ROUTE[0], comment=None).status_code == 409
    assert row(factory).status == "PENDING_APPROVAL"

    assert act(client, "REJECT", OAR_ROUTE[0], "Usage is too low to justify Min-Max").status_code == 200
    assert row(factory).status == "REJECTED"


def test_the_rejection_reason_is_in_the_ledger_and_on_the_recommendation_list(client, factory):
    seed(factory)
    submit(client)
    act(client, "REJECT", OAR_ROUTE[0], "Usage is too low to justify Min-Max")

    history = client.get(f"{BASE}/recommendations/REC-OAR-1/approval-history").json()["items"]
    listed = client.get(f"{BASE}/recommendations", params={"status": "REJECTED"}).json()["items"]

    assert history[-1]["action"] == "REJECT" and history[-1]["comment"] == "Usage is too low to justify Min-Max"
    assert len(listed) == 1
    assert listed[0]["rejection_reason"] == "Usage is too low to justify Min-Max"
    assert listed[0]["manually_converted"] is True and listed[0]["requested_by"] == "planner-1"
    assert listed[0]["conversion_eligibility"] == "NOT_ELIGIBLE"


def test_a_row_that_is_not_rejected_carries_no_rejection_reason(client, factory):
    seed(factory)
    submit(client)

    listed = client.get(f"{BASE}/recommendations").json()["items"]

    assert listed[0]["rejection_reason"] is None
    assert listed[0]["manually_converted"] is True


# --- resubmission after a rejection --------------------------------------------------------------


def test_a_rejected_manual_conversion_can_be_requested_again(client, factory):
    seed(factory)
    submit(client, actor="planner-1")
    act(client, "REJECT", OAR_ROUTE[0], "Usage is too low")

    response = submit(client, actor="planner-2")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PENDING_APPROVAL"
    assert body["chain_index"] == 0 and body["pending_role"] == OAR_ROUTE[0].value
    saved = row(factory)
    assert saved.manually_converted is True and saved.requested_by == "planner-2"
    assert saved.conversion_eligibility == "NOT_ELIGIBLE"


def test_resubmission_keeps_the_earlier_request_in_the_ledger_and_adds_a_new_entry(client, factory):
    seed(factory)
    submit(client, actor="planner-1")
    act(client, "REJECT", OAR_ROUTE[0], "Usage is too low")
    submit(client, actor="planner-2")

    entries = ledger(factory)

    assert [(e.action, e.actor_id, e.previous_status, e.new_status) for e in entries] == [
        ("SUBMIT", "planner-1", "READY_FOR_REVIEW", "PENDING_APPROVAL"),
        ("REJECT", "approver-1", "PENDING_APPROVAL", "REJECTED"),
        ("RESUBMIT", "planner-2", "REJECTED", "PENDING_APPROVAL"),
    ]
    assert entries[1].comment == "Usage is too low"


def test_a_second_rejection_shows_the_newest_reason_and_keeps_the_first_in_the_history(client, factory):
    seed(factory)
    submit(client)
    act(client, "REJECT", OAR_ROUTE[0], "First reason")
    submit(client)
    act(client, "REJECT", OAR_ROUTE[0], "Second reason")

    listed = client.get(f"{BASE}/recommendations", params={"status": "REJECTED"}).json()["items"]
    history = client.get(f"{BASE}/recommendations/REC-OAR-1/approval-history").json()["items"]

    assert listed[0]["rejection_reason"] == "Second reason"
    assert [e["comment"] for e in history if e["action"] == "REJECT"] == ["First reason", "Second reason"]


def test_a_rejected_request_can_go_through_the_whole_chain_the_second_time(client, factory):
    seed(factory)
    submit(client)
    act(client, "REJECT", OAR_ROUTE[0], "Not now")
    submit(client)

    for role in OAR_ROUTE:
        assert act(client, "APPROVE", role).status_code == 200

    assert row(factory).status == "SAP_EXECUTION_PENDING"


def test_a_not_eligible_material_declined_before_submission_can_still_be_requested(client, factory):
    seed(factory)
    client.post(f"{BASE}/recommendations/REC-OAR-1/reject", json={"actor_id": "planner-1", "comment": "Not adopting"})
    assert row(factory).status == "REJECTED"

    response = submit(client, actor="planner-1")

    assert response.status_code == 200
    assert row(factory).manually_converted is True


def test_a_rejected_eligible_recommendation_stays_final(client, factory):
    """Resubmission is only for manual conversions: every other REJECTED recommendation keeps its meaning."""
    seed(factory, verdict="ELIGIBLE")
    submit(client)
    act(client, "REJECT", OAR_ROUTE[0], "No")

    assert submit(client).status_code == 409
    assert row(factory).status == "REJECTED"


def test_a_rejected_stock_item_stays_final(client, factory):
    seed(factory, verdict=None, is_oar=False)
    submit(client)
    first_role = routing.route_for(False, None, PolicyDocument())[0]
    act(client, "REJECT", first_role, "No")

    assert submit(client).status_code == 409
    assert row(factory).status == "REJECTED"

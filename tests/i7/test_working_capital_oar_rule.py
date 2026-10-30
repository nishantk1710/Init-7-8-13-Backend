"""The OAR rule in the Working Capital Impact, the per-row ZAR figure, the corrected blocking reason and the migration.

SQLite with only the tables the calculation reads, as in test_working_capital_currency.py.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.initiatives.i7.kpi_formulas.resolved import load_active
from app.initiatives.i7.kpi_formulas.working_capital import REQUEST_COUNTED_STATUSES, working_capital_impact
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import builder, ledger
from app.initiatives.i7.recommendations.types import LifecycleStatus
from app.models.i7_features import MaterialFeature
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation
from app.models.i7_staging import StagedMaterial, StagedMaterialPlant, StagedStock, StagingRun
from app.models.i7_stockout_risk import StockoutRiskInput
from app.schemas.i7.recommendations import RecommendationDetail, RecommendationSummary
from tests.i7.test_recommendation_ready_for_review_gate import UNSIGNED_POLICY, oar_row

TABLES = (Recommendation, ApprovalLedgerEntry, StagedMaterial, StagedMaterialPlant, StagedStock, StagingRun, StockoutRiskInput, MaterialFeature)

# A pending request, an approved one and every state after approval all count; a rejected one does not.
LIVE_STATUSES = ["PENDING_APPROVAL", "HELD", "SENT_BACK", "ADJUSTED", "APPROVED", "SAP_EXECUTION_PENDING", "SAP_EXECUTED",
                 "ADOPTED", "PARTIALLY_ADOPTED", "NOT_ADOPTED"]


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in TABLES:
        model.__table__.to_metadata(metadata).indexes.clear()
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def add(session, material, *, verdict=None, manual=False, status="READY_FOR_REVIEW", currency="ZAR", price="100",
        current="5", recommended="2", recommended_max="9"):
    session.add_all([
        Recommendation(
            recommendation_id=f"REC-{material}", sap_material_number=material, sap_plant_code="1300", feature_run_id=1,
            forecast_run_id=1, inventory_run_id=1, oar_run_id=1, policy_id="p", policy_version=1, formula_version="f",
            status=status, impact_status="AVAILABLE", is_oar=verdict is not None, conversion_eligibility=verdict,
            manually_converted=manual, unit_price=Decimal(price), current_rop=Decimal(current),
            recommended_rop=Decimal(recommended), recommended_safety_stock=Decimal("1"),
            recommended_max_stock=Decimal(recommended_max) if recommended_max is not None else None,
            generated_at=datetime(2026, 10, 14, tzinfo=timezone.utc), updated_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
            chain_index=0, adjustment_count=0, current_version=1,
        ),
        StagedMaterialPlant(sap_material_number=material, sap_plant_code="1300", unit_price=Decimal(price), currency=currency,
                            staging_run_id=1, source_table="n_mbew"),
    ])
    session.commit()


def impact(session):
    return working_capital_impact(session, select(Recommendation), load_active(session))


# --- who counts -----------------------------------------------------------------------------------


def test_an_automatically_eligible_oar_material_counts(db):
    add(db, "A", verdict="ELIGIBLE")

    result = impact(db)

    assert result.amount == Decimal("300") and (result.included_count, result.excluded_count) == (1, 0)


def test_a_not_eligible_oar_material_with_no_request_is_left_out(db):
    add(db, "A", verdict="NOT_ELIGIBLE", manual=False)

    result = impact(db)

    assert result.amount is None and (result.included_count, result.excluded_count) == (0, 1)


@pytest.mark.parametrize("status", LIVE_STATUSES)
def test_a_manual_request_counts_from_submission_and_through_approval_and_adoption(db, status):
    add(db, "A", verdict="NOT_ELIGIBLE", manual=True, status=status)

    result = impact(db)

    assert result.amount == Decimal("300") and result.included_count == 1


def test_a_rejected_manual_request_is_left_out(db):
    add(db, "A", verdict="NOT_ELIGIBLE", manual=True, status="REJECTED")

    result = impact(db)

    assert result.amount is None and (result.included_count, result.excluded_count) == (0, 1)


def test_a_rejected_eligible_oar_material_is_left_out(db):
    add(db, "A", verdict="ELIGIBLE", status="REJECTED")

    assert impact(db).included_count == 0


def test_a_manual_request_that_is_back_to_ready_for_review_is_not_a_live_request(db):
    add(db, "A", verdict="NOT_ELIGIBLE", manual=True, status="READY_FOR_REVIEW")

    assert impact(db).included_count == 0


def test_the_counted_statuses_are_every_state_but_rejected_and_the_two_before_a_request():
    assert set(REQUEST_COUNTED_STATUSES) == set(LIVE_STATUSES)
    assert not {"REJECTED", "READY_FOR_REVIEW", "NOT_EVALUABLE"} & set(REQUEST_COUNTED_STATUSES)


# --- stock items and the currency rule are unchanged ---------------------------------------------


@pytest.mark.parametrize("status", ["READY_FOR_REVIEW", "PENDING_APPROVAL", "APPROVED", "REJECTED"])
def test_a_stock_item_counts_whatever_its_status_as_before(db, status):
    add(db, "A", verdict=None, status=status)

    assert impact(db).included_count == 1


def test_a_recommendation_with_an_undetermined_verdict_is_not_affected_by_the_oar_rule(db):
    add(db, "A", verdict="UNKNOWN")

    assert impact(db).included_count == 1


def test_a_manual_request_in_another_currency_is_still_left_out(db):
    add(db, "A", verdict="NOT_ELIGIBLE", manual=True, status="PENDING_APPROVAL", currency="USD")

    result = impact(db)

    assert result.amount is None and (result.included_count, result.excluded_count) == (0, 1)


def test_a_manual_request_in_nad_counts_one_for_one(db):
    add(db, "A", verdict="NOT_ELIGIBLE", manual=True, status="PENDING_APPROVAL", currency="NAD", price="10")

    assert impact(db).amount == Decimal("30")


def test_every_case_together_adds_up_and_is_counted(db):
    add(db, "STOCK", verdict=None)                                                          # counts: 300
    add(db, "ELIGIBLE", verdict="ELIGIBLE")                                                 # counts: 300
    add(db, "NO_REQUEST", verdict="NOT_ELIGIBLE")                                           # left out
    add(db, "PENDING", verdict="NOT_ELIGIBLE", manual=True, status="PENDING_APPROVAL")      # counts: 300
    add(db, "APPROVED", verdict="NOT_ELIGIBLE", manual=True, status="SAP_EXECUTION_PENDING")  # counts: 300
    add(db, "REJECTED", verdict="NOT_ELIGIBLE", manual=True, status="REJECTED")             # left out

    result = impact(db)

    assert result.amount == Decimal("1200")
    assert (result.included_count, result.excluded_count, result.population) == (4, 2, 6)


def test_a_pending_request_counts_the_moment_it_is_submitted(db):
    add(db, "A", verdict="NOT_ELIGIBLE", manual=False)
    assert impact(db).included_count == 0

    recommendation = db.execute(select(Recommendation)).scalar_one()
    ledger.submit_for_approval(db, recommendation, actor_id="planner-1", policy=PolicyDocument())
    db.commit()

    result = impact(db)
    assert recommendation.status == LifecycleStatus.PENDING_APPROVAL.value
    assert result.included_count == 1 and result.amount == Decimal("300")


# --- the per-row figure ---------------------------------------------------------------------------


def _row(**overrides) -> Recommendation:
    values = dict(
        recommendation_id="R", sap_material_number="A", sap_plant_code="1300", policy_id="p", policy_version=1,
        formula_version="f", status="READY_FOR_REVIEW", impact_status="AVAILABLE", unit_price=Decimal("10"),
        current_rop=Decimal("5"), recommended_rop=Decimal("2"), generated_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 14, tzinfo=timezone.utc), chain_index=0, adjustment_count=0, current_version=1,
        manually_converted=False,
    )
    values.update(overrides)
    return Recommendation(**values)


@pytest.mark.parametrize("currency,expected", [("ZAR", "ZAR"), ("NAD", "ZAR"), ("USD", None), (None, None)])
def test_the_row_figure_is_stated_in_zar_only_when_its_price_can_be_(currency, expected):
    summary = RecommendationSummary.from_model(_row(), ("End User",), currency=currency)

    assert summary.working_capital_impact == Decimal("30")
    assert summary.working_capital_currency == expected


def test_a_row_with_no_figure_has_no_currency_on_it():
    summary = RecommendationSummary.from_model(_row(unit_price=None), (), currency="ZAR")

    assert summary.working_capital_impact is None and summary.working_capital_currency is None


def test_the_detail_carries_the_same_currency_and_the_request_fields():
    requested = datetime(2026, 10, 30, tzinfo=timezone.utc)
    # RecommendationDetail reads many columns; the stand-in has them all, and only these matter here.
    full = _full_row(
        manually_converted=True, requested_by="planner-1", requested_at=requested,
        unit_price=Decimal("10"), current_rop=Decimal("5"), recommended_rop=Decimal("2"),
    )
    detail = RecommendationDetail.from_model(full, currency="NAD")

    assert detail.working_capital_currency == "ZAR"
    assert detail.manually_converted is True and detail.requested_by == "planner-1" and detail.requested_at == requested


def _full_row(**state) -> SimpleNamespace:
    """A stand-in with every attribute RecommendationDetail.from_model reads (None where the test does not care)."""
    names = [
        "recommendation_id", "sap_material_number", "sap_plant_code", "circuit", "unit_price", "status", "impact_status",
        "current_safety_stock", "current_rop", "current_max_stock", "recommended_safety_stock", "recommended_rop",
        "recommended_max_stock", "demand_class", "history_status", "baseline_model", "forecast_rate", "lead_time_method",
        "lead_time_days", "lead_time_variance_days", "criticality", "service_level", "z_factor", "is_oar",
        "oar_similarity_status", "oar_estimate_status", "oar_neighbour_count", "oar_best_similarity", "confidence",
        "conversion_eligibility", "conversion_trigger", "conversion_detail", "consumption_count_12m",
        "consumption_count_months", "consumption_count_threshold", "production_impact", "i13_hod_approved",
        "safety_stock_delta", "rop_delta", "max_stock_delta", "rationale_text", "rationale_source", "policy_id",
        "policy_version", "formula_version", "feature_run_id", "forecast_run_id", "inventory_run_id", "oar_run_id",
        "blocking_reason", "safety_stock_method", "max_stock_strategy", "chain_index", "adjustment_count",
        "current_version", "generated_at", "updated_at", "manually_converted", "requested_by", "requested_at",
    ]
    values = {name: None for name in names}
    values.update(
        recommendation_id="R", sap_material_number="A", sap_plant_code="1300", status="READY_FOR_REVIEW", policy_id="p",
        policy_version=1, formula_version="f", impact_status="AVAILABLE", chain_index=0, adjustment_count=0,
        current_version=1, generated_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
    )
    values.update({k: v for k, v in state.items() if k in names})
    return SimpleNamespace(**values)


# --- the blocking reason --------------------------------------------------------------------------


def test_a_search_that_completed_with_no_neighbours_is_reported_as_no_similar_materials_found():
    row = oar_row(neighbour_count=0, eligible_candidates=0, best_similarity=None, status="SUCCESS",
                  estimate_status="NOT_EVALUABLE_NO_NEIGHBORS")

    result = builder.build_oar_recommendation(row, UNSIGNED_POLICY)

    assert result.status is LifecycleStatus.NOT_EVALUABLE
    assert result.blocking_reason == "NO_SIMILAR_MATERIALS_FOUND" != "SUCCESS"
    assert result.recommended_rop is None  # still no values: nothing to convert


def test_the_searchs_own_blocker_is_kept_when_it_names_one():
    row = oar_row(neighbour_count=0, status="NO_ELIGIBLE_NEIGHBORS", estimate_status="NOT_EVALUABLE_NO_NEIGHBORS")

    assert builder.build_oar_recommendation(row, UNSIGNED_POLICY).blocking_reason == "NO_ELIGIBLE_NEIGHBORS"


def test_no_status_at_all_still_falls_back_to_no_eligible_neighbours():
    row = oar_row(neighbour_count=0, status=None, estimate_status="NOT_EVALUABLE_NO_NEIGHBORS")

    assert builder.build_oar_recommendation(row, UNSIGNED_POLICY).blocking_reason == "NO_ELIGIBLE_NEIGHBORS"


def test_the_other_blockers_are_unchanged():
    few = oar_row(neighbour_count=2, estimate_status="NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS")

    assert builder.build_oar_recommendation(few, UNSIGNED_POLICY).blocking_reason == "NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS"


# --- the migration --------------------------------------------------------------------------------


def _migration():
    path = next(Path(__file__).resolve().parents[2].joinpath("alembic", "versions").glob("a6d2f81c9e47_*.py"))
    spec = importlib.util.spec_from_file_location("migration_a6d2f81c9e47", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_extends_the_previous_head_and_adds_the_three_columns():
    module = _migration()

    assert module.revision == "a6d2f81c9e47" and module.down_revision == "f4b8d2a71c63"
    columns = Recommendation.__table__.c
    assert {"manually_converted", "requested_by", "requested_at"} <= set(columns.keys())
    assert columns["manually_converted"].nullable is False
    assert columns["requested_by"].nullable is True and columns["requested_at"].nullable is True


def test_there_is_a_single_migration_head():
    versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    revisions, parents = set(), set()
    for file in versions.glob("*.py"):
        text = file.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("revision"):
                revisions.add(line.split("=", 1)[1].strip().strip("'\""))
            if line.startswith("down_revision"):
                value = line.split("=", 1)[1].strip()
                parents.update(part.strip().strip("'\"") for part in value.strip("()[] ").split(",") if part.strip().strip("'\"") not in ("", "None"))
    assert revisions - parents == {"a6d2f81c9e47"}


def test_an_existing_recommendation_is_not_a_manual_request(db):
    """Rows written before the columns existed read back as not requested, with no requester."""
    add(db, "OLD", verdict="NOT_ELIGIBLE")

    saved = db.execute(select(Recommendation)).scalar_one()

    assert saved.manually_converted is False and saved.requested_by is None and saved.requested_at is None

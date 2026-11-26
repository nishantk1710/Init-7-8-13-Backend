"""The Overview's Working Capital views for Stock item to OAR.

Four ways to read the Stock item population's release:

* ``released``            the existing view: a stock item whose own recommendation lowers its reorder point.
* ``released_stock_oar``  Release Value (Stock + Stock item to OAR): each material once -- an eligible-to-convert one at its
                          release if converted to OAR, every other at its Stock item release.
* ``released_overlap``    Release Up (Overlap): the Stock item release PLUS the OAR release for eligible materials, so a
                          material in both counts twice.
* ``tied_up``             the existing view, unchanged.

SQLite with only the tables the calculation reads, so it runs without a database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.i7 import recommendations as api
from app.initiatives.i7.kpi_formulas.resolved import load_active
from app.initiatives.i7.kpi_formulas.working_capital import working_capital_impact
from app.models.i7_features import MaterialFeature
from app.models.i7_recommendation import Recommendation, SapAdoptionResult
from app.models.i7_reverse import (
    RUN_SKIPPED,
    RUN_SUCCEEDED,
    ReverseConversionRequest,
    ReverseRecommendation,
    ReverseRun,
)
from app.models.i7_staging import StagedMaterial, StagedMaterialPlant, StagedStock, StagingRun

TABLES = (
    Recommendation, SapAdoptionResult, StagedMaterial, StagedMaterialPlant, StagedStock, StagingRun, MaterialFeature,
    ReverseRun, ReverseRecommendation, ReverseConversionRequest,
)
NOW = datetime(2026, 11, 26, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in TABLES:
        model.__table__.to_metadata(metadata).indexes.clear()
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def stock(session, material, *, price="100", current="5", recommended="2", currency="ZAR", is_oar=False, plant="1300"):
    """One stock-item recommendation, and the staged price row that states its currency."""
    session.add(
        Recommendation(
            recommendation_id=f"REC-{material}-{plant}", sap_material_number=material, sap_plant_code=plant,
            feature_run_id=1, forecast_run_id=1, inventory_run_id=1, oar_run_id=1, policy_id="p", policy_version=1,
            formula_version="f", status="READY_FOR_REVIEW", impact_status="AVAILABLE", is_oar=is_oar,
            unit_price=Decimal(price), current_rop=Decimal(current),
            recommended_rop=Decimal(recommended) if recommended is not None else None,
            generated_at=NOW, updated_at=NOW, chain_index=0, adjustment_count=0, current_version=1,
        )
    )
    session.add(
        StagedMaterialPlant(
            sap_material_number=material, sap_plant_code=plant, unit_price=Decimal(price), currency=currency,
            staging_run_id=1, source_table="n_mbew",
        )
    )
    session.commit()


def run(session, status=RUN_SUCCEEDED):
    row = ReverseRun(status=status)
    session.add(row)
    session.commit()
    return row


def verdict(session, run_row, material, *, category="Recommended for OAR", confidence="High", plant="1300"):
    session.add(
        ReverseRecommendation(
            run_id=run_row.id, sort_order=0, sap_material_number=material, sap_plant_code=plant, category=category,
            reason="sparse_recurring_demand", detail="x", decision_rule=12, confidence=confidence,
            issues_latest_12m=1, months_with_use_12m=1, issues_prior_12m=0, lifetime_months_with_use=3, lead_time_bucket="0-30d",
        )
    )
    session.commit()


def request(session, material, status, plant="1300"):
    session.add(
        ReverseConversionRequest(
            sap_material_number=material, sap_plant_code=plant, status=status, reason="r", detail="d", submitted_by="p"
        )
    )
    session.commit()


def amount(session, mode):
    return working_capital_impact(session, select(Recommendation), load_active(session), stock_to_oar=mode)


def members(session, value_change):
    statement = api._apply_filters(
        select(Recommendation.sap_material_number), status=None, plant=None, material=None, demand_class=None, is_oar=None,
        confidence=None, criticality=None, generated_from=None, generated_to=None, value_change=value_change,
    )
    return sorted(session.scalars(statement))


@pytest.fixture
def mix(db):
    """A: stock release 300, eligible (OAR release 500).  B: stock release 150, not eligible.
    C: stock ties up (-100), eligible (OAR release 300).  D: not eligible, ties up.  E: eligible but ROP already 0."""
    stock(db, "A", price="100", current="5", recommended="2")  # 100 x (5 - 2) = 300; if converted 100 x 5 = 500
    stock(db, "B", price="50", current="4", recommended="1")  # 50 x 3 = 150
    stock(db, "C", price="100", current="2", recommended="3")  # 100 x (2 - 3) = -100; if converted 100 x 2 = 200
    stock(db, "D", price="10", current="1", recommended="4")  # 10 x -3 = -30
    stock(db, "E", price="100", current="0", recommended="0")  # nothing to release
    latest = run(db)
    for material in ("A", "C", "E"):
        verdict(db, latest, material, confidence="Medium")
    return db


# --- The existing views are untouched ---------------------------------------------------------------


def test_the_existing_calculation_is_unchanged_when_no_stock_to_oar_view_is_asked(mix):
    result = working_capital_impact(mix, select(Recommendation), load_active(mix))
    assert result.amount == Decimal("300") + Decimal("150") + Decimal("-100") + Decimal("-30")  # 320, as before
    assert result.overlap_count == 0


def test_the_release_and_tie_up_populations_are_what_they_were(mix):
    assert members(mix, "released") == ["A", "B"]
    assert members(mix, "tied_up") == ["C", "D"]


# --- Release Value (Stock + Stock item to OAR): each material once ---------------------------------------


def test_stock_plus_oar_population_is_the_eligible_materials_that_would_release_and_the_other_stock_items_that_release(mix):
    # A (eligible, would release 500), C (eligible, would release 200), B (not eligible, releases 150).
    # D ties up and is not eligible; E is eligible but has no reorder point to release.
    assert members(mix, "released_stock_oar") == ["A", "B", "C"]


def test_stock_plus_oar_values_an_eligible_material_at_its_oar_release_instead_of_its_stock_release(mix):
    scoped = select(Recommendation).where(Recommendation.sap_material_number.in_(members(mix, "released_stock_oar")))
    result = working_capital_impact(mix, scoped, load_active(mix), stock_to_oar="replace")
    assert result.amount == Decimal("500") + Decimal("200") + Decimal("150")  # A and C at OAR release, B at its own
    assert (result.included_count, result.excluded_count, result.overlap_count) == (3, 0, 0)


def test_stock_plus_oar_counts_every_material_once_so_it_is_never_more_than_the_overlap_view(mix):
    once = working_capital_impact(
        mix, select(Recommendation).where(Recommendation.sap_material_number.in_(members(mix, "released_stock_oar"))),
        load_active(mix), stock_to_oar="replace",
    )
    twice = working_capital_impact(
        mix, select(Recommendation).where(Recommendation.sap_material_number.in_(members(mix, "released_overlap"))),
        load_active(mix), stock_to_oar="add",
    )
    assert once.amount < twice.amount


# --- Release Value (Stock item to OAR only): just the eligible materials, at their OAR release ----------


def test_oar_only_population_is_the_eligible_materials_that_would_release_and_nothing_else(mix):
    # A and C are eligible and have a reorder point to release; E is eligible but has none; B and D are not eligible.
    assert members(mix, "released_oar_only") == ["A", "C"]


def test_oar_only_values_each_at_its_oar_release_and_never_counts_a_stock_release(mix):
    scoped = select(Recommendation).where(Recommendation.sap_material_number.in_(members(mix, "released_oar_only")))
    result = working_capital_impact(mix, scoped, load_active(mix), stock_to_oar="replace")
    assert result.amount == Decimal("500") + Decimal("200")  # A and C at OAR release; B's 150 is not in it
    assert (result.included_count, result.excluded_count, result.overlap_count) == (2, 0, 0)


def test_oar_only_leaves_out_a_rejected_conversion(db):
    stock(db, "R", price="10", current="3", recommended="1")
    latest = run(db)
    verdict(db, latest, "R", confidence="High")
    assert members(db, "released_oar_only") == ["R"]
    request(db, "R", status="REJECTED")
    assert members(db, "released_oar_only") == []


# --- Release Up (Overlap): the stock release and the OAR release, a material in both counted twice ------


def test_overlap_population_is_every_releasing_stock_item_plus_every_eligible_one_that_would_release(mix):
    assert members(mix, "released_overlap") == ["A", "B", "C"]  # A and B already release; C joins as eligible


def test_overlap_adds_the_oar_release_on_top_of_the_stock_release(mix):
    scoped = select(Recommendation).where(Recommendation.sap_material_number.in_(members(mix, "released_overlap")))
    result = working_capital_impact(mix, scoped, load_active(mix), stock_to_oar="add")
    # A: 300 (stock) + 500 (OAR) = 800   B: 150 (stock only)   C: 0 (it ties up as a stock item) + 200 (OAR)
    assert result.amount == Decimal("800") + Decimal("150") + Decimal("200")
    assert result.overlap_count == 1  # only A is counted in both


def test_overlap_equals_the_existing_release_view_plus_the_oar_release_of_the_eligible(mix):
    existing = working_capital_impact(
        mix, select(Recommendation).where(Recommendation.sap_material_number.in_(members(mix, "released"))), load_active(mix)
    ).amount
    scoped = select(Recommendation).where(Recommendation.sap_material_number.in_(members(mix, "released_overlap")))
    combined = working_capital_impact(mix, scoped, load_active(mix), stock_to_oar="add").amount
    assert combined == existing + Decimal("500") + Decimal("200")  # the eligible ones' OAR release: A and C


# --- Which materials are eligible -------------------------------------------------------------------


def test_high_medium_and_low_confidence_all_count_as_eligible(db):
    for material in ("H", "M", "L"):
        stock(db, material, price="10", current="3", recommended="3")
    latest = run(db)
    for material, level in (("H", "High"), ("M", "Medium"), ("L", "Low")):
        verdict(db, latest, material, confidence=level)
    assert members(db, "released_stock_oar") == ["H", "L", "M"]


def test_a_material_that_is_not_eligible_does_not_count(db):
    stock(db, "N", price="10", current="3", recommended="3")
    verdict(db, run(db), "N", category="Not Eligible", confidence=None)
    assert members(db, "released_stock_oar") == []


def test_only_the_newest_run_decides(db):
    stock(db, "X", price="10", current="3", recommended="3")
    old = run(db)
    verdict(db, old, "X")
    run(db)  # a newer run, in which X is not stored as eligible
    assert members(db, "released_stock_oar") == []


def test_a_skipped_or_failed_run_is_not_a_run_to_read(db):
    stock(db, "X", price="10", current="3", recommended="3")
    good = run(db)
    verdict(db, good, "X")
    run(db, status=RUN_SKIPPED)  # newer, but it stored nothing
    assert members(db, "released_stock_oar") == ["X"]


@pytest.mark.parametrize(("status", "counted"), [("REJECTED", False), ("PENDING_APPROVAL", True), ("SAP_EXECUTION_PENDING", True), ("HELD", True)])
def test_a_rejected_conversion_request_takes_a_material_out(db, status, counted):
    stock(db, "R", price="10", current="3", recommended="3")
    verdict(db, run(db), "R")
    request(db, "R", status)
    assert (members(db, "released_stock_oar") == ["R"]) is counted


def test_a_material_rejected_once_and_asked_for_again_counts_again(db):
    stock(db, "R", price="10", current="3", recommended="3")
    verdict(db, run(db), "R")
    request(db, "R", "REJECTED")
    request(db, "R", "PENDING_APPROVAL")  # the newer request is the one that counts
    assert members(db, "released_stock_oar") == ["R"]


def test_the_same_material_at_another_plant_is_judged_on_its_own(db):
    stock(db, "P", price="10", current="3", recommended="3", plant="1300")
    stock(db, "P", price="10", current="3", recommended="3", plant="1500")
    verdict(db, run(db), "P", plant="1300")
    scoped = api._apply_filters(
        select(Recommendation.sap_plant_code), status=None, plant=None, material="P", demand_class=None, is_oar=None,
        confidence=None, criticality=None, generated_from=None, generated_to=None, value_change="released_stock_oar",
    )
    assert list(db.scalars(scoped)) == ["1300"]


# --- Stock items only, ROP only, and the money rule --------------------------------------------------


def test_an_oar_material_is_never_in_a_stock_item_to_oar_view(db):
    stock(db, "O", price="10", current="3", recommended="1", is_oar=True)
    verdict(db, run(db), "O")
    assert members(db, "released_stock_oar") == [] and members(db, "released_overlap") == []


def test_the_release_is_the_reorder_point_only_not_the_maximum_stock(db):
    stock(db, "A", price="10", current="4", recommended="4")
    verdict(db, run(db), "A")
    scoped = select(Recommendation).where(Recommendation.sap_material_number == "A")
    assert working_capital_impact(db, scoped, load_active(db), stock_to_oar="replace").amount == Decimal("40")  # 10 x ROP 4


def test_the_zar_and_nad_rule_still_applies_in_the_new_views(db):
    stock(db, "Z", price="100", current="2", recommended="2", currency="ZAR")
    stock(db, "N", price="10", current="2", recommended="2", currency="NAD")
    stock(db, "U", price="10", current="2", recommended="2", currency="USD")
    latest = run(db)
    for material in ("Z", "N", "U"):
        verdict(db, latest, material)
    result = working_capital_impact(db, select(Recommendation), load_active(db), stock_to_oar="replace")
    assert result.amount == Decimal("200") + Decimal("20")  # USD is left out, never converted
    assert (result.included_count, result.excluded_count) == (2, 1)


def test_a_placeholder_price_is_not_a_price_in_the_new_views(db):
    stock(db, "T", price="1.00", current="5", recommended="5")
    verdict(db, run(db), "T")
    result = working_capital_impact(db, select(Recommendation), load_active(db), stock_to_oar="replace")
    assert (result.amount, result.included_count) == (None, 0)


def test_a_missing_run_means_nobody_is_eligible_and_the_views_fall_back_to_the_stock_release(db):
    stock(db, "B", price="50", current="4", recommended="1")
    assert members(db, "released_stock_oar") == ["B"] == members(db, "released_overlap")
    scoped = select(Recommendation)
    assert working_capital_impact(db, scoped, load_active(db), stock_to_oar="replace").amount == Decimal("150")
    assert working_capital_impact(db, scoped, load_active(db), stock_to_oar="add").amount == Decimal("150")


# --- The endpoints accept the two new values -----------------------------------------------------------


def test_every_endpoint_that_takes_the_value_change_filter_takes_the_two_new_values():
    from app.main import app

    paths = app.openapi()["paths"]
    seen = 0
    for path, operations in paths.items():
        for operation in operations.values():
            for parameter in operation.get("parameters", []):
                if parameter["name"] == "value_change":
                    seen += 1
                    options = {v for branch in parameter["schema"].get("anyOf", [parameter["schema"]]) for v in branch.get("enum", [])}
                    assert options == {"released", "tied_up", "released_stock_oar", "released_overlap", "released_oar_only"}, path
    assert seen >= 4  # the list, the summary, the export and the adoption summary


def test_the_summary_says_how_many_materials_the_overlap_view_counts_twice():
    from app.schemas.i7.recommendations import RecommendationSummaryStats

    assert RecommendationSummaryStats.model_fields["working_capital_overlap_count"].default == 0

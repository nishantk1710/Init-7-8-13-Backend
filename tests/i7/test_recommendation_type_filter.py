"""The Type filter: OAR, Declining and Normal -- three groups that never overlap.

OAR is decided first (sized from similar materials). Of the rest, Declining is a
material-plant the declining-demand rule flagged in the feature run the
recommendation was built from; everything else is Normal.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.i7 import recommendations as api
from app.models.i7_features import DecliningDemandEvaluation
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedMaterial


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (Recommendation, DecliningDemandEvaluation, StagedMaterial):
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def rec(material, *, is_oar=False, run=1, plant="1300"):
    return Recommendation(
        recommendation_id=f"REC-{material}-{plant}",
        sap_material_number=material,
        sap_plant_code=plant,
        feature_run_id=run,
        forecast_run_id=run,
        inventory_run_id=run,
        oar_run_id=run,
        policy_id="p",
        policy_version=1,
        formula_version="f",
        status="READY_FOR_REVIEW",
        impact_status="X",
        is_oar=is_oar,
        recommended_rop=10,
        generated_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
    )


def evaluation(material, status="DECLINING_DEMAND", *, run=1, plant="1300"):
    return DecliningDemandEvaluation(
        feature_run_id=run, sap_material_number=material, sap_plant_code=plant, status=status,
        reason="r", rule_version="declining-v1", window_start=date(2025, 8, 1), window_end=date(2026, 7, 1),
        months_evaluated=12,
    )


def seed(db):
    db.add_all([
        rec("A-OAR", is_oar=True),
        rec("B-DECLINING"),
        rec("C-NORMAL"),
        rec("D-NULL", is_oar=None),                 # an unknown OAR flag is not OAR
        rec("E-OAR-FLAGGED", is_oar=True),          # OAR wins: never also "declining"
        rec("F-OTHER-RUN"),                         # flagged, but in a different feature run
        rec("G-NOT-FLAGGED"),
    ])
    db.add_all([
        evaluation("B-DECLINING"),
        evaluation("E-OAR-FLAGGED"),
        evaluation("F-OTHER-RUN", run=2),
        evaluation("G-NOT-FLAGGED", "RECENT_DEMAND_PRESENT"),
    ])
    db.commit()


def listing(db, demand_type=None):
    response = api.list_recommendations(
        session=db, page=1, page_size=100, sort="generated_at", sort_desc=False, status=None, plant=None,
        material=None, demand_class=None, is_oar=None, confidence=None, criticality=None,
        generated_from=None, generated_to=None, demand_type=demand_type,
    )
    return response


def materials(response):
    return sorted(item.material for item in response.items)


def test_oar_lists_the_material_plants_sized_from_similar_materials(db):
    seed(db)
    assert materials(listing(db, "oar")) == ["A-OAR", "E-OAR-FLAGGED"]


def test_declining_lists_only_flagged_material_plants_that_are_not_oar(db):
    seed(db)
    assert materials(listing(db, "declining")) == ["B-DECLINING"]


def test_normal_is_everything_else_including_an_unknown_oar_flag(db):
    seed(db)
    assert materials(listing(db, "normal")) == ["C-NORMAL", "D-NULL", "F-OTHER-RUN", "G-NOT-FLAGGED"]


def test_the_three_groups_never_overlap_and_together_are_everything(db):
    seed(db)
    groups = [set(materials(listing(db, kind))) for kind in ("oar", "declining", "normal")]
    assert groups[0].isdisjoint(groups[1]) and groups[0].isdisjoint(groups[2]) and groups[1].isdisjoint(groups[2])
    assert groups[0] | groups[1] | groups[2] == set(materials(listing(db)))


def test_totals_follow_the_filter(db):
    seed(db)
    assert (listing(db, "oar").total, listing(db, "declining").total, listing(db, "normal").total) == (2, 1, 4)
    assert listing(db).total == 7


def test_a_flag_from_another_feature_run_does_not_count(db):
    seed(db)
    # F-OTHER-RUN was flagged in run 2, but its recommendation was built from run 1.
    assert "F-OTHER-RUN" not in materials(listing(db, "declining"))


def test_each_row_says_which_group_it_is_in(db):
    seed(db)
    by_material = {item.material: item.demand_type for item in listing(db).items}
    assert by_material == {
        "A-OAR": "OAR", "B-DECLINING": "DECLINING", "C-NORMAL": "NORMAL", "D-NULL": "NORMAL",
        "E-OAR-FLAGGED": "OAR", "F-OTHER-RUN": "NORMAL", "G-NOT-FLAGGED": "NORMAL",
    }


def test_a_database_without_the_evaluation_table_still_lists_everything_as_normal(db):
    seed(db)
    DecliningDemandEvaluation.__table__.drop(db.get_bind())
    response = listing(db)
    assert len(response.items) == 7
    assert {item.demand_type for item in response.items} == {"OAR", "NORMAL"}


def test_without_a_type_nothing_is_filtered(db):
    seed(db)
    assert len(listing(db).items) == 7

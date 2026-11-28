"""The recommendation list search matches each word, in any order, in the number or the description."""

from __future__ import annotations

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.i7 import recommendations as api
from app.models.i7_features import DecliningDemandEvaluation
from app.models.i7_recommendation import Recommendation
from app.models.i7_reverse import ReverseConversionRequest, ReverseRecommendation, ReverseRun
from app.models.i7_staging import StagedMaterial, StagedMaterialPlant

from tests.i7.test_recommendation_type_filter import rec


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (
        Recommendation, DecliningDemandEvaluation, StagedMaterial, StagedMaterialPlant,
        ReverseRun, ReverseRecommendation, ReverseConversionRequest,
    ):
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()
    metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def staged(material, description):
    return StagedMaterial(sap_material_number=material, description=description, source_table="MARA", staging_run_id=1)


def found(db, text):
    response = api.list_recommendations(
        session=db, page=1, page_size=100, sort="generated_at", sort_desc=False, status=None, plant=None,
        material=None, demand_class=None, is_oar=None, confidence=None, criticality=None,
        generated_from=None, generated_to=None, demand_type=None, search=text,
    )
    return sorted(item.material for item in response.items)


@pytest.fixture
def seeded(db):
    db.add_all([rec("000000000000100001"), rec("000000000000100002"), rec("000000000000100003"), rec("000000000000100004")])
    db.add_all([
        staged("000000000000100001", 'ORIFICE, PRESSURE 1 PLUG, 2MM 1/8"'),
        staged("000000000000100002", "GEAR GIRTH BALLMILL"),
        staged("000000000000100003", "HOSE"),
    ])  # 100004 has no staged description at all
    db.commit()
    return db


def test_every_word_must_be_found_in_any_order(seeded):
    assert found(seeded, "orifice pressure 1 plug 2mm") == ["000000000000100001"]
    assert found(seeded, "2mm plug orifice") == ["000000000000100001"]
    assert found(seeded, "gear girth ball mill") == ["000000000000100002"]
    assert found(seeded, "orifice hose") == []


def test_a_word_may_be_in_the_number_and_another_in_the_description(seeded):
    assert found(seeded, "100002 girth") == ["000000000000100002"]


def test_a_material_number_still_matches_without_a_description(seeded):
    assert found(seeded, "100004") == ["000000000000100004"]


def test_blank_or_punctuation_only_search_is_no_filter(seeded):
    assert len(found(seeded, "   ")) == 4
    assert len(found(seeded, " , ")) == 4

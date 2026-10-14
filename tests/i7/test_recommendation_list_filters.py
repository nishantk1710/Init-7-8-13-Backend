"""Every filter on the Recommendations list searches the whole catalogue, not one page.

The material search, criticality, demand pattern, status and stockout risk used to be applied
by the browser to the 50 rows it had been sent, so a material on page 200 could never be found
and the footer count (the server's) contradicted the table (the browser's). They are database
filters now, and ``total`` follows them.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.dialects import mssql
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.i7 import recommendations as api
from app.models.i7_features import DecliningDemandEvaluation
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedMaterial

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


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


_counter = {"n": 0}


def rec(material, *, status="READY_FOR_REVIEW", criticality=None, demand_class=None, plant="1300"):
    _counter["n"] += 1
    return Recommendation(
        recommendation_id=f"REC-{material}-{plant}", sap_material_number=material, sap_plant_code=plant,
        feature_run_id=1, forecast_run_id=1, inventory_run_id=1, oar_run_id=1, policy_id="p",
        policy_version=1, formula_version="f", status=status, impact_status="X", criticality=criticality,
        demand_class=demand_class, generated_at=BASE + timedelta(minutes=_counter["n"]),
    )


def described(material, description):
    return StagedMaterial(sap_material_number=material, description=description, staging_run_id=1, source_table="n_mara")


def listing(db, *, page=1, page_size=50, **filters):
    args = dict(
        status=None, plant=None, material=None, demand_class=None, is_oar=None, confidence=None,
        criticality=None, generated_from=None, generated_to=None, demand_type=None, search=None,
        criticality_level=None, status_group=None, risk=None,
    )
    args.update(filters)
    return api.list_recommendations(
        session=db, page=page, page_size=page_size, sort="generated_at", sort_desc=True, **args
    )


def materials(response):
    return sorted(item.material for item in response.items)


# --- Material search -----------------------------------------------------------


def test_a_material_far_beyond_the_first_page_is_found_by_search(db):
    # 120 materials, newest first: the one we want is the oldest, on page 3 of 50.
    db.add(rec("000000008000005029"))
    db.add_all([rec(f"00000000{n:010d}") for n in range(1, 120)])
    db.commit()
    assert "000000008000005029" not in materials(listing(db, page=1))
    found = listing(db, search="8000005029")
    assert materials(found) == ["000000008000005029"]
    assert found.total == 1


def test_a_partial_number_matches_every_material_containing_it_and_the_total_counts_them_all(db):
    db.add_all([rec(f"00000000{n:010d}") for n in range(8000005020, 8000005030)])
    db.add(rec("000000001111111111"))
    db.commit()
    result = listing(db, search="80000050", page_size=3)
    assert result.total == 10 and len(result.items) == 3
    assert len(listing(db, search="80000050", page=4, page_size=3).items) == 1  # the 10th match, on page 4


def test_search_matches_the_description_whatever_its_case(db):
    db.add_all([rec("M1"), rec("M2"), described("M1", "ECCENTRIC HYDROCONE ASSY 60"), described("M2", "Gasket")])
    db.commit()
    assert materials(listing(db, search="hydrocone")) == ["M1"]


def test_search_treats_percent_and_underscore_as_ordinary_characters(db):
    db.add_all([rec("M1"), rec("M_2"), described("M1", "100% pure")])
    db.commit()
    assert materials(listing(db, search="%")) == ["M1"]
    assert materials(listing(db, search="_")) == ["M_2"]


def test_a_blank_search_filters_nothing(db):
    db.add_all([rec("M1"), rec("M2")])
    db.commit()
    assert listing(db, search="   ").total == 2


def test_a_material_in_two_plants_is_listed_once_per_plant_not_multiplied_by_the_description_match(db):
    db.add_all([rec("M1"), rec("M1", plant="1500"), described("M1", "Valve")])
    db.commit()
    result = listing(db, search="valve")
    assert result.total == 2 and sorted(item.plant for item in result.items) == ["1300", "1500"]


# --- Criticality ----------------------------------------------------------------


def criticality_db(db):
    db.add_all([
        rec("C", criticality="CRITICAL"), rec("H", criticality="IMPACT"), rec("M", criticality="INSURANCE"),
        rec("N", criticality="NORMAL"), rec("O", criticality="OBSOLETE"), rec("U"), rec("X", criticality="WEIRD"),
    ])
    db.commit()


@pytest.mark.parametrize(
    "level,expected",
    [("Critical", ["C"]), ("High", ["H"]), ("Medium", ["M"]), ("Low", ["N", "O", "U", "X"])],
)
def test_criticality_level_follows_the_tier_mapping_the_table_uses(db, level, expected):
    criticality_db(db)
    result = listing(db, criticality_level=level)
    assert materials(result) == expected and result.total == len(expected)


# --- Status ---------------------------------------------------------------------


def test_status_group_finds_every_backend_status_behind_the_label(db):
    db.add_all([
        rec("A", status="PENDING_APPROVAL"), rec("B", status="HELD"), rec("C", status="ADJUSTED"),
        rec("D", status="READY_FOR_REVIEW"), rec("E", status="SENT_BACK"), rec("F", status="APPROVED"),
        rec("G", status="SAP_EXECUTION_PENDING"), rec("H", status="REJECTED"), rec("I", status="SAP_EXECUTED"),
        rec("J", status="ADOPTED"), rec("K", status="NOT_EVALUABLE"), rec("L", status="SOMETHING_NEW"),
    ])
    db.commit()
    assert materials(listing(db, status_group="In Approval")) == ["A", "B", "C"]
    assert materials(listing(db, status_group="Returned")) == ["E"]
    assert materials(listing(db, status_group="Approved")) == ["F", "G"]
    assert materials(listing(db, status_group="Rejected")) == ["H"]
    assert materials(listing(db, status_group="Implemented")) == ["I", "J"]
    # An unrecognised status shows as Pending Review in the table, so it is found there.
    assert materials(listing(db, status_group="Pending Review")) == ["D", "K", "L"]


# --- Stockout risk --------------------------------------------------------------


def test_risk_is_only_ever_raised_by_a_recommendation_still_awaiting_a_decision(db):
    db.add_all([
        rec("OPEN-CRIT", criticality="CRITICAL"),
        rec("OPEN-IMPACT", status="PENDING_APPROVAL", criticality="IMPACT"),
        rec("OPEN-INS", status="SENT_BACK", criticality="INSURANCE"),
        rec("OPEN-NORMAL", criticality="NORMAL"),
        rec("OPEN-NOTIER"),
        rec("DONE-CRIT", status="APPROVED", criticality="CRITICAL"),
    ])
    db.commit()
    assert materials(listing(db, risk="critical")) == ["OPEN-CRIT"]
    assert materials(listing(db, risk="high")) == ["OPEN-IMPACT"]
    assert materials(listing(db, risk="medium")) == ["OPEN-INS"]
    assert materials(listing(db, risk="low")) == ["DONE-CRIT", "OPEN-NORMAL", "OPEN-NOTIER"]


# --- Demand pattern and combining -----------------------------------------------


def test_demand_pattern_and_the_other_filters_combine_and_the_total_follows(db):
    db.add_all([
        rec("A", demand_class="LUMPY", criticality="CRITICAL"),
        rec("B", demand_class="LUMPY", criticality="NORMAL"),
        rec("C", demand_class="SMOOTH", criticality="CRITICAL"),
        rec("D", demand_class="LUMPY", criticality="CRITICAL", plant="1500"),
    ])
    db.commit()
    result = listing(db, demand_class="LUMPY", criticality_level="Critical", plant="1300")
    assert materials(result) == ["A"] and result.total == 1


def test_filters_apply_to_the_newest_row_of_each_material_plant_only(db):
    old = rec("M1", criticality="CRITICAL")
    old.recommendation_id = "REC-M1-old"
    old.feature_run_id = 0
    db.add(old)
    db.add(rec("M1", criticality="NORMAL"))  # the newer row: M1 is no longer critical
    db.commit()
    assert listing(db, criticality_level="Critical").total == 0


# --- The SQL the database receives ----------------------------------------------


def test_the_new_filters_compile_for_sql_server_without_a_window_function_or_is_one(db):
    db.add(rec("M1", criticality="CRITICAL"))
    db.commit()
    dialect = mssql.dialect()
    dialect._supports_offset_fetch = True
    seen: list[str] = []
    real = db.execute

    def recording(statement, *args, **kw):
        seen.append(str(statement.compile(dialect=dialect, compile_kwargs={"literal_binds": True})))
        return real(statement, *args, **kw)

    db.execute = recording  # type: ignore[method-assign]
    listing(db, search="M1", criticality_level="Low", status_group="Pending Review", risk="critical",
            demand_class="LUMPY")
    joined = "\n".join(seen)
    assert not re.search(r"row_number", joined, re.I)
    assert not re.search(r"\bIS 1\b", joined)
    assert "LIKE" in joined.upper()

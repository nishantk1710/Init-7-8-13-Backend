"""The recommendations list stays fast on the real table.

On the real database the list took ~55 s for one row and the OAR / Normal filters did not
finish in 150 s. Two things were the cost: ranking the whole table with a window function
to find the newest row per material-plant, and counting and sorting thousands of full-width
rows (they carry long text columns) to return one page. The list now takes the newest row
as the highest id per material-plant, and filters, counts, sorts and pages on the id alone.
Only the page's own rows are loaded in full.
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


def rec(material, *, run=1, day=0, plant="1300"):
    return Recommendation(
        recommendation_id=f"REC-{material}-{plant}", sap_material_number=material, sap_plant_code=plant,
        feature_run_id=run, forecast_run_id=run, inventory_run_id=run, oar_run_id=run, policy_id="p",
        policy_version=1, formula_version="f", status="READY_FOR_REVIEW", impact_status="X",
        rationale_text="a long explanation " * 20, generated_at=BASE + timedelta(days=day),
    )


def listing(db, *, page=1, page_size=50, sort_desc=True, **filters):
    return api.list_recommendations(
        session=db, page=page, page_size=page_size, sort="generated_at", sort_desc=sort_desc, status=None,
        plant=None, material=None, demand_class=None, is_oar=None, confidence=None, criticality=None,
        generated_from=None, generated_to=None, demand_type=filters.get("demand_type"),
    )


def test_the_newest_row_for_a_material_plant_is_the_one_listed(db):
    db.add_all([rec("M1", run=1, day=0), rec("M1", run=2, day=1), rec("M2", run=1, day=0)])
    db.commit()
    response = listing(db)
    assert response.total == 2
    assert sorted((i.material, i.recommendation_id) for i in response.items) == [
        ("M1", "REC-M1-1300"), ("M2", "REC-M2-1300"),
    ]
    m1 = next(i for i in response.items if i.material == "M1")
    assert m1.generated_at.replace(tzinfo=timezone.utc) == BASE + timedelta(days=1)  # the second run's row


def test_pages_follow_each_other_without_gaps_or_repeats(db):
    db.add_all([rec(f"M{i}", day=i) for i in range(5)])
    db.commit()
    pages = [listing(db, page=n, page_size=2) for n in (1, 2, 3)]
    assert [p.total for p in pages] == [5, 5, 5]
    seen = [i.material for p in pages for i in p.items]
    assert seen == ["M4", "M3", "M2", "M1", "M0"]  # newest first, each once
    assert listing(db, page=4, page_size=2).items == []


def test_ascending_order_is_the_reverse(db):
    db.add_all([rec(f"M{i}", day=i) for i in range(3)])
    db.commit()
    assert [i.material for i in listing(db, sort_desc=False).items] == ["M0", "M1", "M2"]


def test_the_page_keeps_the_order_of_the_ids_it_was_cut_from(db):
    # Rows are loaded by id afterwards; the page must still come back in sort order.
    db.add_all([rec("M-old", day=0), rec("M-new", day=9), rec("M-mid", day=5)])
    db.commit()
    assert [i.material for i in listing(db).items] == ["M-new", "M-mid", "M-old"]


# --- the SQL the database receives -------------------------------------------------------------


def statements_run(db, **kwargs):
    seen: list[str] = []
    real = db.execute
    dialect = mssql.dialect()
    dialect._supports_offset_fetch = True  # what a connection to a current SQL Server sets: paging is OFFSET / FETCH

    def recording(statement, *args, **kw):
        seen.append(str(statement.compile(dialect=dialect, compile_kwargs={"literal_binds": True})))
        return real(statement, *args, **kw)

    db.execute = recording  # type: ignore[method-assign]
    listing(db, **kwargs)
    return seen


@pytest.mark.parametrize("demand_type", [None, "oar", "declining", "normal"])
def test_no_window_function_is_used_to_find_the_newest_row(db, demand_type):
    db.add_all([rec("M1")])
    db.commit()
    assert not [sql for sql in statements_run(db, demand_type=demand_type) if re.search(r"row_number", sql, re.I)]


@pytest.mark.parametrize("demand_type", [None, "oar", "declining", "normal"])
def test_counting_and_paging_never_touch_the_wide_text_columns(db, demand_type):
    row = rec("M1")
    row.is_oar = demand_type == "oar"
    db.add(row)
    if demand_type == "declining":
        db.add(DecliningDemandEvaluation(
            feature_run_id=1, sap_material_number="M1", sap_plant_code="1300", status="DECLINING_DEMAND",
            reason="r", rule_version="declining-v1", months_evaluated=12,
        ))
    db.commit()
    queries = statements_run(db, demand_type=demand_type)
    narrow = [sql for sql in queries if "rationale_text" not in sql]
    wide = [sql for sql in queries if "rationale_text" in sql]
    assert len(wide) == 1, "only the page's own rows may be loaded in full"
    assert "IN (" in wide[0] and "OFFSET" not in wide[0].upper()  # loaded by id, not sorted
    assert any("OFFSET" in sql.upper() for sql in narrow)  # the sort and the page are on ids

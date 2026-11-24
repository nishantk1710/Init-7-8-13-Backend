"""When the I07 results were last rebuilt: the date and time the Inventory Planning page shows.

A rebuild is finished when the recommendations are written, so the newest recommendation is the moment; a
successful pipeline run that finished later counts too. Read-only.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.i7.runs import get_last_rebuilt, last_rebuilt_at
from app.models.i7_pipeline import STATUS_FAILED, STATUS_RUNNING, STATUS_SUCCEEDED, PipelineRun
from app.models.i7_recommendation import Recommendation
from tests.i7.test_stockout_risk import recommendation

BUILT = datetime(2026, 11, 23, 18, 4, 13, tzinfo=timezone.utc)


def _engine(with_pipeline_table: bool = True):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    models = (Recommendation, PipelineRun) if with_pipeline_table else (Recommendation,)
    for model in models:
        model.__table__.to_metadata(metadata).indexes.clear()
    metadata.create_all(engine)
    return engine


@pytest.fixture
def db():
    with Session(_engine()) as session:
        yield session


def _run(status, finished):
    return PipelineRun(
        status=status, trigger_reason="manual", snapshot_complete=False, started_at=finished - timedelta(hours=1),
        finished_at=finished,
    )


def test_nothing_built_yet_is_none_not_a_made_up_date(db):
    assert last_rebuilt_at(db) is None
    assert get_last_rebuilt(db).rebuilt_at is None


def test_it_is_the_newest_recommendation_written(db):
    db.add_all([
        recommendation("A", generated_at=BUILT - timedelta(days=7)),
        recommendation("B", generated_at=BUILT),
        recommendation("C", generated_at=BUILT - timedelta(hours=3)),
    ])
    db.commit()
    assert last_rebuilt_at(db) == BUILT
    assert get_last_rebuilt(db).rebuilt_at == BUILT


def test_a_rebuild_run_by_hand_has_no_pipeline_run_and_the_recommendations_decide(db):
    db.add(recommendation("A", generated_at=BUILT))
    db.commit()
    assert last_rebuilt_at(db) == BUILT


def test_a_later_successful_pipeline_run_counts(db):
    db.add(recommendation("A", generated_at=BUILT))
    db.add(_run(STATUS_SUCCEEDED, BUILT + timedelta(minutes=9)))
    db.commit()
    assert last_rebuilt_at(db) == BUILT + timedelta(minutes=9)


def test_a_failed_or_running_pipeline_run_is_not_a_rebuild(db):
    db.add(recommendation("A", generated_at=BUILT))
    db.add_all([_run(STATUS_FAILED, BUILT + timedelta(hours=2)), _run(STATUS_RUNNING, BUILT + timedelta(hours=3))])
    db.commit()
    assert last_rebuilt_at(db) == BUILT


def test_an_earlier_pipeline_run_does_not_move_it_back(db):
    db.add(recommendation("A", generated_at=BUILT))
    db.add(_run(STATUS_SUCCEEDED, BUILT - timedelta(days=1)))
    db.commit()
    assert last_rebuilt_at(db) == BUILT


def test_a_database_without_the_pipeline_run_table_still_gives_the_recommendation_date():
    with Session(_engine(with_pipeline_table=False)) as session:
        session.add(recommendation("A", generated_at=BUILT))
        session.commit()
        assert last_rebuilt_at(session) == BUILT


def test_a_naive_database_timestamp_is_read_as_utc(db):
    db.add(recommendation("A", generated_at=BUILT.replace(tzinfo=None)))
    db.add(_run(STATUS_SUCCEEDED, BUILT - timedelta(minutes=5)))
    db.commit()
    result = last_rebuilt_at(db)
    assert result.tzinfo is not None
    assert result == BUILT


# --- through the real app: the route exists, is read-only and answers in UTC ----------------


def test_the_endpoint_answers_through_the_api():
    from fastapi.testclient import TestClient

    from app.core.db import get_db
    from app.main import app

    engine = _engine()
    with Session(engine) as seed:
        seed.add(recommendation("A", generated_at=BUILT))
        seed.commit()

    def _session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = _session
    try:
        response = TestClient(app).get("/api/v1/i7/last-rebuilt")
        assert response.status_code == 200
        stamp = datetime.fromisoformat(response.json()["rebuilt_at"].replace("Z", "+00:00"))
        assert stamp == BUILT
        assert TestClient(app).post("/api/v1/i7/last-rebuilt").status_code == 405  # read-only
    finally:
        app.dependency_overrides.pop(get_db, None)

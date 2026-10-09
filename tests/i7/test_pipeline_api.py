"""Phase D: pipeline-run status over the API, and the snapshot verdict
reaching ``snapshot_complete``.

The API tests use FastAPI's TestClient with the session dependency overridden
onto an in-memory database -- the same shape the other I07 route tests use, and
enough to prove the response contract without a server.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.i7.deps import get_session
from app.initiatives.i7 import pipeline as pipeline_module
from app.main import create_app
from app.models.base import Base
from app.models.i7_pipeline import (
    STATUS_ABANDONED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    PipelineRun,
)

BASE = datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc)
PREFIX = "/api/v1/i7"


@pytest.fixture
def factory():
    # StaticPool keeps ONE connection, so every session sees the same in-memory
    # database. The default pool hands out a fresh connection per checkout, and
    # an SQLite ``:memory:`` database belongs to its connection -- the table
    # created here would simply not exist in the session the request uses.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine, tables=[PipelineRun.__table__])
    return sessionmaker(bind=engine)


@pytest.fixture
def client(factory):
    app = create_app()

    def _session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    # No ``with``: entering the context manager runs the lifespan, which
    # rebuilds normalise views and starts snapshot builds against the real
    # database. These tests are about the response contract, and the session
    # they need is the overridden one. tests/i7/api/test_i7_api.py constructs
    # its client the same way for the same reason.
    yield TestClient(app)
    app.dependency_overrides.clear()


def _run(factory, **overrides) -> int:
    values = dict(
        status=STATUS_SUCCEEDED,
        trigger_reason="source data changed",
        source_fingerprint="raw_marc:41:2026-10-09",
        snapshot_complete=True,
        stage_statuses=(
            "staging=succeeded;features=succeeded;forecasting=succeeded;"
            "inventory=succeeded;oar=succeeded;recommendations=succeeded"
        ),
        staging_run_id=10,
        feature_run_id=11,
        forecast_run_id=12,
        inventory_run_id=13,
        oar_run_id=14,
        recommendations_written=42,
        started_at=BASE,
        finished_at=BASE + timedelta(minutes=9),
    )
    values.update(overrides)
    with factory() as session:
        row = PipelineRun(**values)
        session.add(row)
        session.commit()
        return row.id


# --- Listing --------------------------------------------------------------


class TestListing:
    def test_an_empty_table_lists_nothing(self, client) -> None:
        response = client.get(f"{PREFIX}/pipeline-runs")

        assert response.status_code == 200
        assert response.json() == {"items": []}

    def test_runs_are_listed_newest_first(self, client, factory) -> None:
        first = _run(factory, trigger_reason="manual")
        second = _run(factory, trigger_reason="retry")

        items = client.get(f"{PREFIX}/pipeline-runs").json()["items"]

        assert [item["run_id"] for item in items] == [second, first]

    def test_the_limit_is_honoured(self, client, factory) -> None:
        for _ in range(3):
            _run(factory)

        items = client.get(f"{PREFIX}/pipeline-runs?limit=2").json()["items"]

        assert len(items) == 2


# --- Detail ---------------------------------------------------------------


class TestDetail:
    def test_every_recorded_field_is_exposed(self, client, factory) -> None:
        run_id = _run(factory)

        body = client.get(f"{PREFIX}/pipeline-runs/{run_id}").json()

        assert body["status"] == STATUS_SUCCEEDED
        assert body["trigger_reason"] == "source data changed"
        assert body["source_fingerprint"] == "raw_marc:41:2026-10-09"
        assert body["snapshot_complete"] is True
        assert body["staging_run_id"] == 10
        assert body["feature_run_id"] == 11
        assert body["forecast_run_id"] == 12
        assert body["inventory_run_id"] == 13
        assert body["oar_run_id"] == 14
        assert body["recommendations_written"] == 42
        assert body["finished_at"] is not None

    def test_stage_statuses_are_parsed_into_objects(self, client, factory) -> None:
        """Stored as text because models/base.py allows no JSONB; the API shape
        should not inherit that storage decision."""
        run_id = _run(factory)

        stages = client.get(f"{PREFIX}/pipeline-runs/{run_id}").json()["stages"]

        assert [s["name"] for s in stages] == [
            "staging", "features", "forecasting", "inventory", "oar", "recommendations",
        ]
        assert {s["status"] for s in stages} == {"succeeded"}

    def test_a_failed_run_names_its_failed_stage(self, client, factory) -> None:
        """The first question an operator asks."""
        run_id = _run(
            factory,
            status=STATUS_FAILED,
            failed_stage="forecasting",
            error="ForecastError: no champion",
            stage_statuses="staging=succeeded;features=succeeded;forecasting=failed",
            forecast_run_id=None,
            recommendations_written=0,
        )

        body = client.get(f"{PREFIX}/pipeline-runs/{run_id}").json()

        assert body["status"] == STATUS_FAILED
        assert body["failed_stage"] == "forecasting"
        assert "no champion" in body["error"]
        assert len(body["stages"]) == 3

    def test_a_run_with_no_stages_yet_returns_an_empty_list(
        self, client, factory
    ) -> None:
        run_id = _run(factory, status=STATUS_RUNNING, stage_statuses=None, finished_at=None)

        body = client.get(f"{PREFIX}/pipeline-runs/{run_id}").json()

        assert body["stages"] == []
        assert body["finished_at"] is None

    def test_an_unknown_run_is_404(self, client) -> None:
        response = client.get(f"{PREFIX}/pipeline-runs/9999")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PIPELINE_RUN_NOT_FOUND"


class TestLatest:
    def test_latest_is_not_parsed_as_an_id(self, client, factory) -> None:
        """Route ordering: /latest is declared before /{run_id}."""
        _run(factory)
        newest = _run(factory, trigger_reason="newest")

        body = client.get(f"{PREFIX}/pipeline-runs/latest").json()

        assert body["run_id"] == newest
        assert body["trigger_reason"] == "newest"

    def test_latest_includes_a_still_running_run(self, client, factory) -> None:
        """An operator asking "what is happening now?" must see the run in
        flight, not the last one that finished."""
        _run(factory)
        running = _run(factory, status=STATUS_RUNNING, finished_at=None)

        body = client.get(f"{PREFIX}/pipeline-runs/latest").json()

        assert body["run_id"] == running
        assert body["status"] == STATUS_RUNNING

    def test_latest_is_404_when_nothing_has_run(self, client) -> None:
        assert client.get(f"{PREFIX}/pipeline-runs/latest").status_code == 404

    def test_an_abandoned_run_is_visible(self, client, factory) -> None:
        """Recovery has to be observable, or a killed run looks like nothing
        happened."""
        _run(factory, status=STATUS_ABANDONED, error="abandoned: ...", finished_at=BASE)

        body = client.get(f"{PREFIX}/pipeline-runs/latest").json()

        assert body["status"] == STATUS_ABANDONED


class TestExistingRoutesAreUnchanged:
    def test_the_stage_run_routes_still_exist(self, client) -> None:
        """Phase D must not alter runs.py's contract."""
        paths = set(client.app.openapi()["paths"])

        assert f"{PREFIX}/runs" in paths
        assert f"{PREFIX}/runs/{{run_type}}/{{run_id}}" in paths

    def test_pipeline_runs_has_its_own_prefix(self, client) -> None:
        """Not /runs/pipeline, which runs.py would match as a run_type."""
        paths = set(client.app.openapi()["paths"])

        assert f"{PREFIX}/pipeline-runs" in paths
        assert f"{PREFIX}/runs/pipeline" not in paths


# --- The snapshot verdict reaching the sweep ------------------------------


class TestSnapshotVerdictGatesTheSweep:
    """The integration Phase C exists for: the resolver's answer, not a
    caller's assertion, decides whether the deactivation sweep runs."""

    @pytest.fixture
    def wired(self, factory, monkeypatch):
        monkeypatch.setattr(pipeline_module, "get_sessionmaker", lambda: factory)
        monkeypatch.setattr("app.core.db.get_sessionmaker", lambda: factory, raising=False)
        return factory

    def _capture(self, monkeypatch, complete: bool):
        captured = {}

        def fake_run_pipeline(**kwargs):
            captured.update(kwargs)
            return pipeline_module.PipelineResult(run_id=1, status=STATUS_SUCCEEDED)

        monkeypatch.setattr(pipeline_module, "run_pipeline", fake_run_pipeline)
        monkeypatch.setattr(
            "app.initiatives.i7.snapshot.resolve_snapshot",
            lambda session: __import__(
                "app.initiatives.i7.snapshot", fromlist=["SnapshotVerdict"]
            ).SnapshotVerdict(complete, sweep_id="S1", reasons=() if complete else ("delta since",)),
        )
        monkeypatch.setattr(
            "app.initiatives.i7.watch.source_fingerprint", lambda session: "fp"
        )
        return captured

    def test_a_verified_snapshot_permits_the_sweep(
        self, wired, monkeypatch
    ) -> None:
        captured = self._capture(monkeypatch, complete=True)

        pipeline_module.run_pipeline_for_current_source()

        assert captured["snapshot_complete"] is True
        assert captured["source_fingerprint"] == "fp"

    def test_an_unverified_snapshot_withholds_the_sweep(
        self, wired, monkeypatch
    ) -> None:
        """A delta, partial sweep, failed refresh or ambiguous provenance all
        arrive here as complete=False, and none may deactivate anything."""
        captured = self._capture(monkeypatch, complete=False)

        pipeline_module.run_pipeline_for_current_source()

        assert captured["snapshot_complete"] is False

    def test_an_unverified_snapshot_still_runs_the_pipeline(
        self, wired, monkeypatch
    ) -> None:
        """Only the sweep is withheld. Staging, forecasting and recommendations
        still run -- a delta refresh is real new data."""
        captured = self._capture(monkeypatch, complete=False)

        result = pipeline_module.run_pipeline_for_current_source()

        assert result.status == STATUS_SUCCEEDED
        assert captured["trigger_reason"] == "source data changed"

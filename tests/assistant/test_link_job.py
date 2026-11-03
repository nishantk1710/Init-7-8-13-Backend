"""The Link reservation job: its steps, its waits, its failures, and the routes around it.

The outside world (delta lock, delta cycle, reservation rows, snapshot builds) is
replaced by small fakes; the row logic runs on SQLite.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.assistant import link_job
from app.initiatives.i13 import session_link
from app.models.base import Base
from app.models.reservation_link_job import ReservationLinkJob

REQUEST = "RRXVC7GKT6"


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[ReservationLinkJob.__table__])
    yield sessionmaker(bind=engine)
    engine.dispose()


class FakeLock:
    """acquire() answers from a script of booleans, then True forever."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.acquired = 0
        self.released = 0

    def acquire(self):
        granted = self.answers.pop(0) if self.answers else True
        self.acquired += granted
        return granted

    def release(self):
        self.released += 1


def _build(*, before=False, outcomes=("building", "ready"), start_ok=True):
    """A snapshot rebuild as the runner sees it: ``before`` = one already running whose data
    predates the pull; ``outcomes`` = what the run started after the pull reports, in turn."""
    state = {"started": [], "outcomes": list(outcomes), "before": before, "polls": 0}

    def building():
        if state["before"]:
            state["before"] = False  # it finishes the first time anyone waits for it
            return True
        return bool(state["started"]) and state["outcomes"][0] == "building"

    def start(reason):
        state["started"].append(reason)
        return start_ok

    def run_since(db, moment):
        if not state["started"]:
            return None
        if len(state["outcomes"]) > 1:
            state["polls"] += 1
            if state["polls"] > 1:
                state["outcomes"].pop(0)
                state["polls"] = 0
        return state["outcomes"][0], "boom" if state["outcomes"][0] == "failed" else None

    return state, building, start, run_since


def _hooks(monkeypatch, *, delta_lock=None, summary=None, i13=None, i08=None, job_lock=None, links=2):
    monkeypatch.setattr(
        session_link, "load_request_lines",
        lambda db, ids: {REQUEST: [session_link.RequestLine(REQUEST, 1, "s1", "8000000269", "1300"),
                                   session_link.RequestLine(REQUEST, 2, "s2", "8000000270", "1300")]},
    )
    i13 = i13 or _build()
    i08 = i08 or _build()
    return link_job.Hooks(
        delta_lock=lambda: delta_lock or FakeLock(),
        run_delta=lambda: summary if summary is not None else {"fetched": 23, "loaded": 23, "rows": 5, "failed": 0, "errors": []},
        reservations=lambda db, material, plant: [{"Matnr": material, "Werks": plant}],
        sync_links=lambda db, rows, scope: SimpleNamespace(added=links // 2, removed=0, kept=0),
        i13_building=i13[1], i13_start=i13[2], i13_run_since=i13[3],
        i08_building=i08[1], i08_start=i08[2], i08_run_since=i08[3],
        job_lock=lambda: job_lock or FakeLock(),
        sleep=lambda seconds: None,
    ), i13, i08


def _create(factory):
    with factory() as db:
        job, created = link_job.create(db, REQUEST, requested_by="tester")
        return job.id, created


def _row(factory, job_id):
    with factory() as db:
        job = db.get(ReservationLinkJob, job_id)
        db.expunge(job)
        return job


def _steps(job):
    return {s["key"]: s for s in json.loads(job.steps)}


# --- the row ------------------------------------------------------------------------


def test_a_press_queues_a_job_and_a_second_press_returns_it(factory):
    first, created = _create(factory)
    assert created
    with factory() as db:
        job = db.get(ReservationLinkJob, first)
        assert job.status == link_job.QUEUED and job.step == link_job.STEP_QUEUE
        assert [s["status"] for s in json.loads(job.steps)] == [link_job.PENDING] * 5
    second, created = _create(factory)
    assert (second, created) == (first, False)


def test_a_lost_job_is_abandoned_and_a_press_starts_a_fresh_one(factory):
    first, _ = _create(factory)
    with factory() as db:
        job = db.get(ReservationLinkJob, first)
        job.status = link_job.RUNNING
        job.heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=link_job.STALE_AFTER_SECONDS + 1)
        steps = json.loads(job.steps)
        steps[1]["status"] = link_job.ACTIVE
        job.steps = json.dumps(steps)
        db.commit()
    second, created = _create(factory)
    assert created and second != first
    old = _row(factory, first)
    assert old.status == link_job.ABANDONED
    assert "Press Link reservation again" in old.error
    assert _steps(old)["delta"]["status"] == link_job.SKIPPED


def test_a_fresh_job_is_not_stale():
    job = ReservationLinkJob(status=link_job.RUNNING, heartbeat_at=datetime.now(timezone.utc))
    assert not link_job.is_stale(job)
    job.status = link_job.SUCCEEDED
    job.heartbeat_at = datetime.now(timezone.utc) - timedelta(days=1)
    assert not link_job.is_stale(job)


# --- the runner ---------------------------------------------------------------------


def test_the_happy_path_walks_every_step(factory, monkeypatch):
    hooks, i13, i08 = _hooks(monkeypatch)
    job_id, _ = _create(factory)
    link_job.run_job(job_id, REQUEST, hooks=hooks, sessionmaker=factory)
    job = _row(factory, job_id)
    assert job.status == link_job.SUCCEEDED and job.step == link_job.STEP_DONE
    assert job.error is None and job.links == 2
    assert job.delta_started_at is not None and job.delta_finished_at is not None
    steps = _steps(job)
    assert all(steps[k]["status"] == link_job.DONE for k in ("queue", "delta", "link", "i13", "i08"))
    assert steps["delta"]["detail"] == "23 data set(s) pulled, 5 row(s) merged"
    assert steps["link"]["detail"] == "2 reservation item(s) linked (2 new)"
    assert i13[0]["started"] == [f"manual: link reservation ({job_id})"]
    assert i08[0]["started"] == [f"manual: link reservation ({job_id})"]


def test_a_running_sap_pull_is_waited_for_then_the_job_pulls_itself(factory, monkeypatch):
    lock = FakeLock(answers=[False, False, True])
    hooks, _, _ = _hooks(monkeypatch, delta_lock=lock)
    job_id, _ = _create(factory)
    seen = []
    real_detail = link_job._Runner._detail
    monkeypatch.setattr(link_job._Runner, "_detail", lambda self, key, detail: (seen.append((key, detail)), real_detail(self, key, detail)))
    link_job.run_job(job_id, REQUEST, hooks=hooks, sessionmaker=factory)
    assert lock.acquired == 1 and lock.released == 1
    assert ("delta", "A SAP pull is already running; waiting for it to finish before pulling again") in seen
    assert _row(factory, job_id).status == link_job.SUCCEEDED


def test_another_job_running_means_this_one_queues_first(factory, monkeypatch):
    hooks, _, _ = _hooks(monkeypatch, job_lock=FakeLock(answers=[False, True]))
    job_id, _ = _create(factory)
    link_job.run_job(job_id, REQUEST, hooks=hooks, sessionmaker=factory)
    job = _row(factory, job_id)
    assert job.status == link_job.SUCCEEDED
    assert _steps(job)["queue"]["detail"] == "Started"


def test_a_failed_pull_fails_the_job_at_that_step(factory, monkeypatch):
    hooks, i13, _ = _hooks(monkeypatch, summary={"fetched": 0, "loaded": 0, "rows": 0, "failed": 1, "errors": ["CPI 503"]})
    job_id, _ = _create(factory)
    link_job.run_job(job_id, REQUEST, hooks=hooks, sessionmaker=factory)
    job = _row(factory, job_id)
    assert job.status == link_job.FAILED
    assert job.error == "The SAP pull failed: CPI 503"
    steps = _steps(job)
    assert steps["delta"]["status"] == link_job.FAILED and steps["delta"]["detail"] == job.error
    assert steps["link"]["status"] == link_job.PENDING
    assert i13[0]["started"] == []


def test_a_partly_failed_pull_still_goes_on(factory, monkeypatch):
    hooks, _, _ = _hooks(monkeypatch, summary={"fetched": 22, "loaded": 22, "rows": 3, "failed": 1, "errors": ["one set"]})
    job_id, _ = _create(factory)
    link_job.run_job(job_id, REQUEST, hooks=hooks, sessionmaker=factory)
    job = _row(factory, job_id)
    assert job.status == link_job.SUCCEEDED
    assert _steps(job)["delta"]["detail"] == "22 data set(s) pulled, 3 row(s) merged, 1 failed"


def test_a_rebuild_that_predates_the_pull_is_waited_out_then_another_is_started(factory, monkeypatch):
    i13 = _build(before=True)
    hooks, _, _ = _hooks(monkeypatch, i13=i13)
    job_id, _ = _create(factory)
    link_job.run_job(job_id, REQUEST, hooks=hooks, sessionmaker=factory)
    assert i13[0]["started"] == [f"manual: link reservation ({job_id})"]
    assert _row(factory, job_id).status == link_job.SUCCEEDED


def test_a_rebuild_that_began_after_the_pull_is_accepted_as_it_is(factory, monkeypatch):
    i13 = _build()
    i13[0]["started"].append("somebody else")  # a run exists after the pull before we ask
    hooks, _, _ = _hooks(monkeypatch, i13=i13)
    job_id, _ = _create(factory)
    link_job.run_job(job_id, REQUEST, hooks=hooks, sessionmaker=factory)
    assert i13[0]["started"] == ["somebody else"]
    job = _row(factory, job_id)
    assert job.status == link_job.SUCCEEDED
    assert _steps(job)["i13"]["status"] == link_job.DONE


def test_a_failed_rebuild_fails_the_job_with_its_error(factory, monkeypatch):
    hooks, _, _ = _hooks(monkeypatch, i13=_build(outcomes=("building", "failed")))
    job_id, _ = _create(factory)
    link_job.run_job(job_id, REQUEST, hooks=hooks, sessionmaker=factory)
    job = _row(factory, job_id)
    assert job.status == link_job.FAILED
    assert job.error == "The OAR utilisation rebuild failed: boom"
    steps = _steps(job)
    assert steps["link"]["status"] == link_job.DONE  # the link is kept: it happened before
    assert steps["i13"]["status"] == link_job.FAILED and steps["i08"]["status"] == link_job.PENDING


def test_an_unexpected_error_is_recorded_not_raised(factory, monkeypatch):
    hooks, _, _ = _hooks(monkeypatch)
    hooks.run_delta = lambda: (_ for _ in ()).throw(RuntimeError("database gone"))
    job_id, _ = _create(factory)
    link_job.run_job(job_id, REQUEST, hooks=hooks, sessionmaker=factory)
    job = _row(factory, job_id)
    assert job.status == link_job.FAILED and job.error == "RuntimeError: database gone"


# --- the routes ---------------------------------------------------------------------


class TestRoutes:
    @pytest.fixture
    def client(self, factory, monkeypatch):
        from fastapi.testclient import TestClient

        from app.api.assistant import requests_router
        from app.core.config import get_settings
        from app.core.db import get_db
        from app.main import create_app

        settings = get_settings().model_copy(
            update={"assistant_requests_enabled": True, "link_reservation_enabled": True, "database_url": "mssql+pyodbc://x"}
        )
        monkeypatch.setattr(
            requests_router.request_service, "load",
            lambda db, request_id, settings: (_ for _ in ()).throw(requests_router.request_service.RequestError("unknown request"))
            if request_id != REQUEST else SimpleNamespace(id=REQUEST),
        )
        started = []
        monkeypatch.setattr(link_job, "start_in_background", lambda job_id, request_id: started.append((job_id, request_id)))
        app = create_app()

        def _db():
            with factory() as session:
                yield session

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_settings] = lambda: settings
        client = TestClient(app)  # no `with`: the lifespan would reach the real database
        client.started = started
        client.settings = settings
        yield client
        app.dependency_overrides.clear()

    def test_a_press_starts_a_job_and_a_second_press_finds_it(self, client):
        first = client.post(f"/api/assistant/requests/{REQUEST}/link-reservation")
        assert first.status_code == 202, first.text
        body = first.json()
        assert body["created"] is True and body["status"] == "queued" and body["step"] == "queue"
        assert [s["key"] for s in body["steps"]] == ["queue", "delta", "link", "i13", "i08"]
        assert client.started == [(body["id"], REQUEST)]

        again = client.post(f"/api/assistant/requests/{REQUEST}/link-reservation")
        assert again.status_code == 202
        assert again.json()["id"] == body["id"] and again.json()["created"] is False
        assert client.started == [(body["id"], REQUEST)]

    def test_the_status_route_shows_the_latest_job(self, client):
        assert client.get(f"/api/assistant/requests/{REQUEST}/link-reservation").status_code == 404
        job_id = client.post(f"/api/assistant/requests/{REQUEST}/link-reservation").json()["id"]
        shown = client.get(f"/api/assistant/requests/{REQUEST}/link-reservation")
        assert shown.status_code == 200 and shown.json()["id"] == job_id

    def test_an_unknown_request_is_404(self, client):
        assert client.post("/api/assistant/requests/NOPE/link-reservation").status_code == 404
        assert client.get("/api/assistant/requests/NOPE/link-reservation").status_code == 404

    def test_switched_off_is_503(self, client):
        client.settings.link_reservation_enabled = False
        assert client.post(f"/api/assistant/requests/{REQUEST}/link-reservation").status_code == 503

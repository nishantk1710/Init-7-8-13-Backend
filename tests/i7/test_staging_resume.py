"""Staging saves each step as it finishes and resumes an interrupted run.

Staging the full extract takes about an hour on the nonprod database, and it
used to be one transaction: nine runs on 12-13 Oct 2026 were rolled back whole
by deploys, restarts and an Azure machine move. These tests pin the fix --
per-step commits, resume from the first unsaved step, dead runs cleared so the
feature build is not blocked -- over an in-memory database. The five steps are
replaced by fakes: what is under test is the run bookkeeping, not the SQL each
step runs (that is covered by test_staging_streaming.py).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.initiatives.i7.adapters import extract
from app.initiatives.i7.adapters.ingestion_policy import ExtractIngestionPolicy
from app.initiatives.i7.features.builder import resolve_staging_run
from app.models.base import Base
from app.models.i7_staging import StagingRejection, StagingRun

COUNTS = {"materials": 10, "material-plants": 20, "stock": 30, "consumption": 40, "purchase orders": 50}


@pytest.fixture
def factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[StagingRun.__table__, StagingRejection.__table__])
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(extract, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(extract, "_current_fingerprint", lambda session: "raw_mseg:41:2026-10-13")
    return factory


class Steps:
    """Fake steps: record which ran, and fail at a chosen one."""

    def __init__(self, monkeypatch, fail_at: str | None = None) -> None:
        self.called: list[str] = []
        self.fail_at = fail_at
        monkeypatch.setattr(
            extract,
            "_STEPS",
            tuple((label, attribute, column, self._step(label)) for label, attribute, column, _ in extract._STEPS),
        )

    def _step(self, label):
        def run(session, run_id, policy, rejections):
            self.called.append(label)
            if label == self.fail_at:
                raise RuntimeError(f"killed during {label}")
            return COUNTS[label]

        return run


class RefusedLock:
    def acquire(self) -> bool:
        return False

    def release(self) -> None:
        raise AssertionError("a lock that was never acquired is never released")


def _stage(**kwargs):
    return extract.stage_extract(ExtractIngestionPolicy(), lock=extract._NoLock(), **kwargs)


def _runs(factory) -> list[StagingRun]:
    with factory() as session:
        return list(session.scalars(select(StagingRun).order_by(StagingRun.id)))


def test_a_clean_run_saves_every_step_and_succeeds(factory, monkeypatch) -> None:
    steps = Steps(monkeypatch)
    result = _stage()
    assert result.status == "succeeded" and result.error is None
    assert steps.called == list(COUNTS)
    [run] = _runs(factory)
    assert (run.status, run.materials_staged, run.purchase_orders_staged) == ("succeeded", 10, 50)
    assert run.source_fingerprint == "raw_mseg:41:2026-10-13"


def test_a_failed_step_keeps_the_steps_before_it(factory, monkeypatch) -> None:
    Steps(monkeypatch, fail_at="consumption")
    result = _stage()
    assert result.status == "failed" and "killed during consumption" in result.error
    [run] = _runs(factory)
    assert run.status == "failed"
    assert (run.materials_staged, run.material_plants_staged, run.stock_staged) == (10, 20, 30)
    assert (run.consumption_staged, run.purchase_orders_staged) == (0, 0)


def test_the_next_call_resumes_from_the_first_unsaved_step(factory, monkeypatch) -> None:
    Steps(monkeypatch, fail_at="consumption")
    first = _stage()

    steps = Steps(monkeypatch)
    second = _stage()
    assert steps.called == ["consumption", "purchase orders"]  # 1-3 skipped
    assert second.run_id == first.run_id  # the same run, finished
    assert second.status == "succeeded"
    assert (second.materials, second.stock, second.consumption, second.purchase_orders) == (10, 30, 40, 50)
    [run] = _runs(factory)
    assert (run.status, run.error, run.consumption_staged) == ("succeeded", None, 40)


def test_a_run_killed_outright_is_resumed_too(factory, monkeypatch) -> None:
    """A process killed by a restart never records a failure: its row still
    says running, with the steps it managed to save."""
    with factory() as session:
        session.add(StagingRun(source="normalise_views", status="running", consumption_movement_types=",".join(
            ExtractIngestionPolicy().consumption.all_movement_types), source_fingerprint="raw_mseg:41:2026-10-13",
            materials_staged=10, material_plants_staged=20))
        session.commit()
    steps = Steps(monkeypatch)
    result = _stage()
    assert steps.called == ["stock", "consumption", "purchase orders"]
    assert result.run_id == 1 and result.status == "succeeded"


def test_a_reload_of_the_raw_layer_starts_a_new_run(factory, monkeypatch) -> None:
    """Resuming across a reload would mix two versions of the data."""
    Steps(monkeypatch, fail_at="stock")
    _stage()
    monkeypatch.setattr(extract, "_current_fingerprint", lambda session: "raw_mseg:42:2026-10-14")
    steps = Steps(monkeypatch)
    result = _stage()
    assert steps.called == list(COUNTS)
    assert result.run_id == 2
    assert [r.status for r in _runs(factory)] == ["failed", "succeeded"]


def test_resume_false_always_starts_fresh(factory, monkeypatch) -> None:
    Steps(monkeypatch, fail_at="stock")
    _stage()
    steps = Steps(monkeypatch)
    assert _stage(resume=False).run_id == 2
    assert steps.called == list(COUNTS)


def test_a_run_that_saved_nothing_is_not_resumed(factory, monkeypatch) -> None:
    Steps(monkeypatch, fail_at="materials")
    _stage()
    assert _stage().run_id == 2


def test_dead_running_runs_are_cleared_so_features_can_build(factory, monkeypatch) -> None:
    """Runs 1-9 on nonprod: killed before any step was saved, each left saying
    running -- and the feature build refuses while any run says running."""
    with factory() as session:
        for _ in range(3):
            session.add(StagingRun(source="normalise_views", status="running"))
        session.commit()
    Steps(monkeypatch)
    result = _stage()
    assert result.run_id == 4 and result.status == "succeeded"
    runs = _runs(factory)
    assert [r.status for r in runs] == ["failed", "failed", "failed", "succeeded"]
    assert all("abandoned" in r.error and "staging run 4" in r.error for r in runs[:3])
    with factory() as session:
        assert resolve_staging_run(session).id == 4


def test_a_held_lock_refuses_without_touching_any_run(factory, monkeypatch) -> None:
    with factory() as session:
        session.add(StagingRun(source="normalise_views", status="running"))
        session.commit()
    steps = Steps(monkeypatch)
    result = extract.stage_extract(ExtractIngestionPolicy(), lock=RefusedLock())
    assert result.status == "failed" and "staging lock" in result.error
    assert steps.called == []
    assert [r.status for r in _runs(factory)] == ["running"]  # a live run is left alone


def test_the_snapshot_sweep_waits_for_every_step(factory, monkeypatch) -> None:
    swept: list[int] = []
    monkeypatch.setattr(extract, "deactivate_unseen", lambda session, run_id: swept.append(run_id) or 0)
    Steps(monkeypatch, fail_at="purchase orders")
    _stage(snapshot_complete=True)
    assert swept == []  # never on a run that stopped part-way
    Steps(monkeypatch)
    _stage(snapshot_complete=True)
    assert swept == [1]

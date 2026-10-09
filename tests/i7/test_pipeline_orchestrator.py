"""Phase B: the I07 pipeline orchestrator.

No database server and no stage services. The orchestrator's job is ORDER,
STOPPING, LOCKING and RECORDING -- none of which needs the real stages, and all
of which is invisible if the real stages run (a six-stage pipeline against Azure
SQL takes minutes and cannot run on a laptop at all).

So the six stage callables are replaced with fakes that record their call order
and return whatever status the test wants, and the SQL Server application lock
is replaced with an in-process stand-in of the same shape. What is NOT proven
here is that ``sp_getapplock`` behaves as assumed, or that the stages work --
both are listed as VNet prerequisites in the Phase B report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.initiatives.i7 import pipeline as pipeline_module
from app.initiatives.i7.pipeline import (
    STAGES,
    PipelineBusy,
    run_pipeline,
)
from app.models.base import Base
from app.models.i7_pipeline import (
    STATUS_ABANDONED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    PipelineRun,
)

SUCCEEDED = "succeeded"
FAILED = "failed"


# --- Doubles --------------------------------------------------------------


class FakeLock:
    """The shape of ``app.ingest.scheduler._Lock``, in process.

    ``held`` is class-level so two instances contend exactly as two workers
    would: the second ``acquire`` returns False while the first holds it.
    """

    held = False

    def __init__(self, grant: bool = True) -> None:
        self._grant = grant
        self.acquired = False
        self.released = False

    def acquire(self) -> bool:
        if not self._grant or FakeLock.held:
            return False
        FakeLock.held = True
        self.acquired = True
        return True

    def release(self) -> None:
        self.released = True
        if self.acquired:
            FakeLock.held = False


@dataclass
class FakeOutcome:
    status: str = SUCCEEDED
    error: str | None = None
    run_id: int | None = 1
    deactivated: int = 0
    recommendations_written: int = 0


class FakeStages:
    """Records call order; returns per-stage outcomes the test chooses."""

    def __init__(self, failing: str | None = None, raising: str | None = None) -> None:
        self.calls: list[str] = []
        self.staging_kwargs: dict = {}
        self._failing = failing
        self._raising = raising

    def _make(self, name: str):
        def call(**kwargs):
            self.calls.append(name)
            if name == "staging":
                self.staging_kwargs = kwargs
            if name == self._raising:
                raise RuntimeError(f"{name} exploded")
            if name == self._failing:
                return FakeOutcome(status=FAILED, error=f"{name} said no")
            return FakeOutcome(
                run_id=STAGES.index(name) + 100,
                recommendations_written=7 if name == "recommendations" else 0,
            )

        return call

    def as_dict(self) -> dict:
        return {name: self._make(name) for name in STAGES}


@pytest.fixture(autouse=True)
def _reset_lock():
    FakeLock.held = False
    yield
    FakeLock.held = False


@pytest.fixture
def session_factory(monkeypatch):
    """An in-memory pipeline-run table, wired in as the orchestrator's own."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[PipelineRun.__table__])
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(pipeline_module, "get_sessionmaker", lambda: factory)
    return factory


@pytest.fixture
def stages(monkeypatch):
    def install(fake: FakeStages) -> FakeStages:
        monkeypatch.setattr(pipeline_module, "_stage_callables", fake.as_dict)
        return fake

    return install


# --- Order ----------------------------------------------------------------


class TestStageOrder:
    def test_all_six_stages_run_in_dependency_order(self, session_factory, stages) -> None:
        fake = stages(FakeStages())

        result = run_pipeline(lock=FakeLock())

        assert fake.calls == list(STAGES)
        assert result.ok
        assert result.status == STATUS_SUCCEEDED

    def test_the_order_is_the_one_the_manual_script_uses(self) -> None:
        """Guards against a reordering that would look harmless: each stage
        reads the previous one's output from the database, so the order IS the
        dependency graph."""
        assert STAGES == (
            "staging",
            "features",
            "forecasting",
            "inventory",
            "oar",
            "recommendations",
        )

    def test_stage_run_ids_are_recorded_against_the_pipeline(
        self, session_factory, stages
    ) -> None:
        stages(FakeStages())

        result = run_pipeline(lock=FakeLock())

        with session_factory() as session:
            row = session.get(PipelineRun, result.run_id)
            assert row.staging_run_id == 100
            assert row.feature_run_id == 101
            assert row.forecast_run_id == 102
            assert row.inventory_run_id == 103
            assert row.oar_run_id == 104
            assert row.recommendations_written == 7


# --- Stopping -------------------------------------------------------------


class TestAFailedStageStopsTheRun:
    """Every later stage reads the failed one's output, so continuing would
    compute from the PREVIOUS run's rows and publish them as this run's."""

    @pytest.mark.parametrize("failing", STAGES)
    def test_no_stage_runs_after_a_failure(self, session_factory, stages, failing) -> None:
        fake = stages(FakeStages(failing=failing))

        result = run_pipeline(lock=FakeLock())

        expected = list(STAGES[: STAGES.index(failing) + 1])
        assert fake.calls == expected
        assert result.status == STATUS_FAILED
        assert result.failed_stage == failing

    @pytest.mark.parametrize("raising", STAGES)
    def test_a_raising_stage_is_recorded_not_propagated(
        self, session_factory, stages, raising
    ) -> None:
        """A failed pipeline is an outcome to read, not an exception to handle
        -- the caller is a timer, and an escaping exception would lose the row."""
        stages(FakeStages(raising=raising))

        result = run_pipeline(lock=FakeLock())

        assert result.status == STATUS_FAILED
        assert result.failed_stage == raising
        assert "exploded" in result.error

    def test_the_failed_stage_is_queryable(self, session_factory, stages) -> None:
        stages(FakeStages(failing="forecasting"))

        result = run_pipeline(lock=FakeLock())

        with session_factory() as session:
            row = session.get(PipelineRun, result.run_id)
            assert row.status == STATUS_FAILED
            assert row.failed_stage == "forecasting"
            assert row.stage_statuses == (
                "staging=succeeded;features=succeeded;forecasting=failed"
            )
            assert row.finished_at is not None

    def test_a_failed_run_is_never_marked_succeeded(self, session_factory, stages) -> None:
        stages(FakeStages(failing="inventory"))

        result = run_pipeline(lock=FakeLock())

        with session_factory() as session:
            succeeded = session.execute(
                select(func.count())
                .select_from(PipelineRun)
                .where(PipelineRun.status == STATUS_SUCCEEDED)
            ).scalar()
        assert succeeded == 0
        assert not result.ok


# --- Locking --------------------------------------------------------------


class TestConcurrency:
    def test_a_second_run_is_refused_while_one_holds_the_lock(
        self, session_factory, stages
    ) -> None:
        stages(FakeStages())
        holder = FakeLock()
        assert holder.acquire() is True

        with pytest.raises(PipelineBusy):
            run_pipeline(lock=FakeLock())

    def test_a_refused_run_writes_no_row(self, session_factory, stages) -> None:
        """Refusal is not a run. Recording one would put a failed-looking row in
        the table every time the timer fired during a long pipeline."""
        stages(FakeStages())
        holder = FakeLock()
        holder.acquire()

        with pytest.raises(PipelineBusy):
            run_pipeline(lock=FakeLock())

        with session_factory() as session:
            assert session.execute(select(func.count()).select_from(PipelineRun)).scalar() == 0

    def test_the_lock_is_released_after_a_successful_run(
        self, session_factory, stages
    ) -> None:
        stages(FakeStages())
        lock = FakeLock()

        run_pipeline(lock=lock)

        assert lock.released is True
        assert FakeLock.held is False

    def test_the_lock_is_released_after_a_failed_run(self, session_factory, stages) -> None:
        """Released in a finally: a stage failure must not wedge the schedule."""
        stages(FakeStages(failing="staging"))
        lock = FakeLock()

        run_pipeline(lock=lock)

        assert lock.released is True
        assert FakeLock.held is False

    def test_the_lock_is_released_when_a_stage_raises(
        self, session_factory, stages
    ) -> None:
        stages(FakeStages(raising="oar"))
        lock = FakeLock()

        run_pipeline(lock=lock)

        assert lock.released is True
        assert FakeLock.held is False

    def test_the_fake_matches_the_real_locks_interface(self) -> None:
        """Without this the tests above prove only that FakeLock works. The
        orchestrator defaults to the real ``_Lock``, so a drift in its
        constructor or method names would be found in production, not here."""
        import inspect

        from app.ingest.scheduler import _Lock

        assert inspect.signature(_Lock.acquire) == inspect.signature(FakeLock.acquire)
        assert inspect.signature(_Lock.release) == inspect.signature(FakeLock.release)
        # Takes a name, so the pipeline can hold a mutex of its own.
        assert "name" in inspect.signature(_Lock.__init__).parameters

    def test_the_pipeline_lock_is_not_an_ingest_lock(self) -> None:
        """A shared name would be a different bug entirely: the pipeline would
        block the delta timer, and a long run would stall ingestion."""
        from app.ingest.scheduler import FULL_REFRESH_LOCK_NAME
        from app.ingest.scheduler import LOCK_NAME as DELTA_LOCK
        from app.initiatives.i7.pipeline import LOCK_NAME as PIPELINE_LOCK

        assert len({DELTA_LOCK, FULL_REFRESH_LOCK_NAME, PIPELINE_LOCK}) == 3


# --- Recovery -------------------------------------------------------------


class TestStaleRunRecovery:
    """A process killed mid-pipeline never writes anything, so its row stays
    RUNNING forever. The lock is the liveness signal; the row is the audit."""

    def _orphan(self, session_factory) -> int:
        with session_factory() as session:
            row = PipelineRun(
                status=STATUS_RUNNING,
                trigger_reason="killed mid-run",
                started_at=datetime.now(timezone.utc),
            )
            session.add(row)
            session.commit()
            return row.id

    def test_a_stale_running_row_is_abandoned_by_the_next_run(
        self, session_factory, stages
    ) -> None:
        orphan_id = self._orphan(session_factory)
        stages(FakeStages())

        run_pipeline(lock=FakeLock())

        with session_factory() as session:
            orphan = session.get(PipelineRun, orphan_id)
            assert orphan.status == STATUS_ABANDONED
            assert orphan.finished_at is not None
            assert "abandoned" in orphan.error

    def test_a_stale_row_does_not_block_the_next_run(
        self, session_factory, stages
    ) -> None:
        """The failure mode this exists to prevent: one killed run leaving the
        pipeline permanently refusing to start."""
        self._orphan(session_factory)
        stages(FakeStages())

        result = run_pipeline(lock=FakeLock())

        assert result.ok

    def test_abandoning_requires_the_lock(self, session_factory) -> None:
        """Guards the invariant directly: reachable from outside the locked
        section, this would mass-abandon a healthy concurrent run."""
        with session_factory() as session:
            with pytest.raises(RuntimeError, match="without the pipeline lock"):
                pipeline_module._abandon_stale(session, lock_held=False)


class TestRetry:
    def test_a_retry_is_a_new_row_and_leaves_the_failure_visible(
        self, session_factory, stages
    ) -> None:
        """Every attempt stays readable. A mutated retry counter on one row
        would overwrite the evidence of what failed the first time."""
        stages(FakeStages(failing="features"))
        first = run_pipeline(lock=FakeLock(), trigger_reason="manual")

        stages(FakeStages())
        second = run_pipeline(lock=FakeLock(), trigger_reason=f"retry of {first.run_id}")

        assert second.run_id != first.run_id
        with session_factory() as session:
            rows = session.execute(select(PipelineRun).order_by(PipelineRun.id)).scalars().all()
            assert [r.status for r in rows] == [STATUS_FAILED, STATUS_SUCCEEDED]
            assert rows[0].failed_stage == "features"
            assert rows[1].trigger_reason == f"retry of {first.run_id}"

    def test_a_successful_retry_does_not_rewrite_the_earlier_attempt(
        self, session_factory, stages
    ) -> None:
        stages(FakeStages(failing="oar"))
        first = run_pipeline(lock=FakeLock())
        stages(FakeStages())
        run_pipeline(lock=FakeLock())

        with session_factory() as session:
            assert session.get(PipelineRun, first.run_id).status == STATUS_FAILED


# --- Snapshot gating ------------------------------------------------------


class TestSnapshotGating:
    """The sweep is destructive-looking and must never fire on a delta."""

    def test_snapshot_complete_defaults_to_false(self, session_factory, stages) -> None:
        fake = stages(FakeStages())

        run_pipeline(lock=FakeLock())

        assert fake.staging_kwargs["snapshot_complete"] is False

    def test_the_flag_is_passed_through_to_staging(self, session_factory, stages) -> None:
        fake = stages(FakeStages())

        run_pipeline(lock=FakeLock(), snapshot_complete=True)

        assert fake.staging_kwargs["snapshot_complete"] is True

    def test_the_decision_is_recorded_on_the_run(self, session_factory, stages) -> None:
        """Visible on the run that made the decision, not only on the staging
        run it produced."""
        stages(FakeStages())

        result = run_pipeline(lock=FakeLock(), snapshot_complete=True)

        with session_factory() as session:
            assert session.get(PipelineRun, result.run_id).snapshot_complete is True

    def test_only_staging_receives_the_flag(self, session_factory, stages) -> None:
        """The other five stages take no such argument; passing one would be a
        TypeError at the worst possible moment."""
        fake = stages(FakeStages())

        run_pipeline(lock=FakeLock(), snapshot_complete=True)

        assert set(fake.staging_kwargs) == {"snapshot_complete", "source_fingerprint"}


# --- Lineage --------------------------------------------------------------


class TestLineage:
    def test_the_fingerprint_is_persisted_and_passed_to_staging(
        self, session_factory, stages
    ) -> None:
        fake = stages(FakeStages())
        fingerprint = "raw_marc:41:2026-10-09T01:00:00+00:00"

        result = run_pipeline(lock=FakeLock(), source_fingerprint=fingerprint)

        assert fake.staging_kwargs["source_fingerprint"] == fingerprint
        with session_factory() as session:
            assert session.get(PipelineRun, result.run_id).source_fingerprint == fingerprint

    def test_last_successful_fingerprint_ignores_failed_runs(
        self, session_factory, stages
    ) -> None:
        """Phase C compares against this. A failed run advancing it would mean a
        refresh that broke the pipeline was never retried."""
        stages(FakeStages())
        run_pipeline(lock=FakeLock(), source_fingerprint="good")
        stages(FakeStages(failing="features"))
        run_pipeline(lock=FakeLock(), source_fingerprint="bad")

        with session_factory() as session:
            assert pipeline_module.last_successful_fingerprint(session) == "good"

    def test_the_trigger_reason_is_recorded(self, session_factory, stages) -> None:
        stages(FakeStages())

        result = run_pipeline(lock=FakeLock(), trigger_reason="source data changed")

        with session_factory() as session:
            row = session.get(PipelineRun, result.run_id)
            assert row.trigger_reason == "source data changed"
            assert row.started_at is not None
            assert row.finished_at is not None

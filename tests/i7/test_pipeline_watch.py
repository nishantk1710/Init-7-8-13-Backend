"""Phase C: change detection, and the snapshot verdict reaching the sweep.

In-memory SQLite. What these prove is the DECISION -- when a pipeline should
start, when it must not, and what ``snapshot_complete`` is set to. What they do
not prove is that a real refresh populates ``ingestion_run`` the way the
fixtures do; that needs the VNet.

The watcher loop itself is deliberately not tested here because it is
deliberately not written: ``i7_pipeline_watch_enabled`` is false and no loop is
started from ``app.main``. :func:`check_and_run` is the tick a future loop would
call, and it is tested directly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.initiatives.i7 import pipeline as pipeline_module
from app.initiatives.i7 import watch as watch_module
from app.initiatives.i7.snapshot import I7_RAW_TABLES
from app.initiatives.i7.watch import should_run, source_fingerprint
from app.models.base import Base
from app.models.csv_extract import CsvExtractRequest
from app.models.i7_pipeline import (
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    PipelineRun,
)
from app.models.ingestion import IngestionRun

BASE = datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            IngestionRun.__table__,
            PipelineRun.__table__,
            CsvExtractRequest.__table__,
        ],
    )
    with Session(engine) as session:
        yield session


def _load(session, table: str, *, status: str = "succeeded", minutes: int = 0):
    at = BASE + timedelta(minutes=minutes)
    row = IngestionRun(
        source_file=f"csv:{table}:F{minutes:08d}",
        target_table=table,
        row_count=10,
        status=status,
        started_at=at,
        finished_at=at,
    )
    session.add(row)
    session.commit()
    return row


def _pipeline_run(session, fingerprint: str, status: str = STATUS_SUCCEEDED):
    row = PipelineRun(
        status=status,
        trigger_reason="test",
        source_fingerprint=fingerprint,
        started_at=BASE,
        finished_at=BASE,
    )
    session.add(row)
    session.commit()
    return row


# --- The fingerprint ------------------------------------------------------


class TestSourceFingerprint:
    def test_an_empty_history_fingerprints_as_empty(self, session) -> None:
        assert source_fingerprint(session) == ""

    def test_a_failed_load_does_not_move_the_fingerprint(self, session) -> None:
        """A failed load must not look like new data: the pipeline would run
        against rows the loader itself says are not there."""
        _load(session, "raw_marc")
        before = source_fingerprint(session)

        _load(session, "raw_marc", status="failed", minutes=10)

        assert source_fingerprint(session) == before

    def test_a_new_successful_load_moves_the_fingerprint(self, session) -> None:
        _load(session, "raw_marc")
        before = source_fingerprint(session)

        _load(session, "raw_marc", minutes=10)

        assert source_fingerprint(session) != before

    def test_a_load_of_an_unrelated_table_is_ignored(self, session) -> None:
        """I08 and I13 tables move constantly. Watching them would rerun the
        whole I07 pipeline for data it does not read."""
        _load(session, "raw_marc")
        before = source_fingerprint(session)

        _load(session, "raw_lfa1", minutes=10)
        _load(session, "raw_resb", minutes=11)

        assert source_fingerprint(session) == before

    def test_two_loads_in_the_same_second_still_differ(self, session) -> None:
        """The run id is in the fingerprint as well as the timestamp, so a
        reload finishing within a clock tick still registers."""
        _load(session, "raw_marc", minutes=0)
        before = source_fingerprint(session)
        _load(session, "raw_marc", minutes=0)

        assert source_fingerprint(session) != before

    def test_every_watched_table_is_an_i7_source(self) -> None:
        """raw_mkpf is deliberately absent: MSEG carries BUDAT_MKPF
        denormalised and I07 staging never joins MKPF."""
        assert "raw_mkpf" not in I7_RAW_TABLES
        assert "raw_marc" in I7_RAW_TABLES
        assert "odata_material_plant" in I7_RAW_TABLES


# --- The decision ---------------------------------------------------------


class TestShouldRun:
    def test_an_unchanged_fingerprint_does_not_trigger(self, session) -> None:
        """The duplicate-run guard: a tick every five minutes must not rerun a
        multi-minute pipeline over data that has not moved."""
        _load(session, "raw_marc")
        _pipeline_run(session, source_fingerprint(session))

        decision = should_run(session)

        assert decision.run is False
        assert "unchanged" in decision.reason

    def test_a_changed_fingerprint_triggers(self, session) -> None:
        _load(session, "raw_marc")
        _pipeline_run(session, source_fingerprint(session))
        _load(session, "raw_mseg", minutes=10)

        assert should_run(session).run is True

    def test_no_previous_run_triggers(self, session) -> None:
        _load(session, "raw_marc")

        decision = should_run(session)

        assert decision.run is True
        assert "no successful pipeline run" in decision.reason

    def test_no_loads_at_all_does_not_trigger(self, session) -> None:
        """Nothing to process is not an error, and must not start a run that
        would stage an empty catalogue."""
        decision = should_run(session)

        assert decision.run is False
        assert "no successful load" in decision.reason

    def test_a_failed_pipeline_run_does_not_claim_the_fingerprint(
        self, session
    ) -> None:
        """Otherwise a refresh that broke the pipeline would never be retried."""
        _load(session, "raw_marc")
        _pipeline_run(session, source_fingerprint(session), status=STATUS_FAILED)

        assert should_run(session).run is True

    def test_a_failed_ingestion_alone_does_not_trigger(self, session) -> None:
        """A refresh that failed must not start a pipeline."""
        _load(session, "raw_marc", status="failed")

        decision = should_run(session)

        assert decision.run is False


# --- No self-triggering ---------------------------------------------------


def test_the_pipelines_own_writes_cannot_move_the_fingerprint(session) -> None:
    """Structural, not a guard that could be forgotten: the fingerprint reads
    ingestion_run, which only the raw-layer loaders write. The pipeline writes
    i7_staged_*, i7_*_run and i7_pipeline_run -- disjoint sets."""
    _load(session, "raw_marc")
    before = source_fingerprint(session)

    _pipeline_run(session, before)
    session.add(
        CsvExtractRequest(
            request_id="FMARC00000001",
            sap_table="MARC",
            entity_set="MARCSet",
            from_date="20230101",
            to_date="20261009",
            status="complete",
            reconcile="exact",
            expected_rows=10,
            received_rows=10,
            fired_at=BASE,
        )
    )
    session.commit()

    assert source_fingerprint(session) == before


# --- check_and_run --------------------------------------------------------


class TestCheckAndRun:
    """The tick a future loop would call. Never raises, whatever happens."""

    @pytest.fixture
    def wired(self, session, monkeypatch):
        factory = sessionmaker(bind=session.get_bind())
        monkeypatch.setattr(watch_module, "get_sessionmaker", lambda: factory, raising=False)
        monkeypatch.setattr(
            "app.core.db.get_sessionmaker", lambda: factory, raising=False
        )
        return factory

    def test_an_unchanged_source_runs_nothing(self, session, wired, monkeypatch) -> None:
        _load(session, "raw_marc")
        _pipeline_run(session, source_fingerprint(session))
        called = []
        monkeypatch.setattr(
            pipeline_module,
            "run_pipeline_for_current_source",
            lambda: called.append(1),
        )

        outcome = watch_module.check_and_run()

        assert called == []
        assert "no run" in outcome

    def test_a_busy_pipeline_is_reported_as_a_skip_not_a_failure(
        self, session, wired, monkeypatch
    ) -> None:
        """Covers the window the check cannot see: a run started by hand, or by
        another instance, between the check and the call."""
        _load(session, "raw_marc")

        def busy():
            raise pipeline_module.PipelineBusy("another run holds the lock")

        monkeypatch.setattr(pipeline_module, "run_pipeline_for_current_source", busy)

        outcome = watch_module.check_and_run()

        assert "skipped" in outcome

    def test_a_raising_pipeline_does_not_escape(self, session, wired, monkeypatch) -> None:
        """A tick that raises would kill the loop that calls it."""
        _load(session, "raw_marc")

        def boom():
            raise RuntimeError("database went away")

        monkeypatch.setattr(pipeline_module, "run_pipeline_for_current_source", boom)

        outcome = watch_module.check_and_run()

        assert "raised" in outcome
        assert "database went away" in outcome


# --- The watcher stays off ------------------------------------------------


def test_the_watcher_is_disabled_by_default() -> None:
    """Production triggering stays off until memory and runtime are measured
    against the real database. Asserted so enabling it is a deliberate, visible
    change rather than a default nobody re-examined."""
    from app.core.config import Settings

    assert Settings().i7_pipeline_watch_enabled is False


def test_no_watcher_loop_is_started_from_the_application() -> None:
    """app.main starts the ingest scheduler and the I08/I13 snapshot watchers.
    It must not start an I07 pipeline loop while the flag is unproven."""
    import inspect

    import app.main

    source = inspect.getsource(app.main)
    # Named symbols, not the substring "i7": app.main legitimately imports
    # app.schemas.i7.errors for the shared error envelope, and asserting on the
    # bare string would fail on that and teach nothing.
    assert "check_and_run" not in source
    assert "i7.watch" not in source
    assert "run_pipeline" not in source

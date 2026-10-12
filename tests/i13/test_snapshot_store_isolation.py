"""The stored I13 build runs isolated: its own process, one at a time, never
started by a page, never cleaned up from under itself, bounded retries, batches
sized by rows -- and it builds without the optional ZMM065 report.

Every case here is one that took the deployed build down on 10-11 Oct 2026.
Uses the seeded tenant from test_snapshot_store (needs a database).
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import delete, func, select, text

from app.core.config import get_settings
from app.initiatives.i13.snapshot_store.builder import cut_batches
from tests.i13.test_snapshot_store import (  # noqa: F401 -- fixtures
    AS_OF,
    db,
    settings,
    tenant,
)

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


def test_batches_close_on_rows_as_well_as_materials() -> None:
    weighted = [("A", 10), ("B", 10), ("C", 500), ("D", 5), ("E", 5), ("F", 5), ("G", 5)]
    batches = cut_batches(weighted, batch_materials=3, batch_rows=100)
    # C alone exceeds the row budget: a batch of its own. D-F reach 3 materials.
    assert [(b.lo, b.hi) for b in batches] == [("A", "B"), ("C", "C"), ("D", "F"), ("G", "G")]
    # Every material is in exactly one batch, in order.
    assert cut_batches([], 3, 100) == []
    assert [(b.lo, b.hi) for b in cut_batches(weighted, batch_materials=100)] == [("A", "G")]


@pytest.fixture()
def clean(db):  # noqa: F811
    """No stored versions, no work tables, no build in this process."""
    from app.initiatives.i13.snapshot_store import lifecycle, work_tables
    from app.models.i13_snapshot_store import I13SnapshotRecord, I13SnapshotRun

    def wipe() -> None:
        db.execute(delete(I13SnapshotRecord))
        db.execute(delete(I13SnapshotRun))
        db.commit()
        work_tables.drop_versions_except(db.connection(), set())
        db.commit()

    wipe()
    lifecycle._state.thread = None
    lifecycle._state.last_error = None
    lifecycle._state.building_since = None
    yield db
    wipe()


@pytest.fixture()
def started(monkeypatch):
    """Record build starts instead of launching them."""
    from app.initiatives.i13.snapshot_store import lifecycle

    calls: list[str] = []
    monkeypatch.setattr(
        lifecycle, "start_background_build", lambda reason, after=None, manual=False: calls.append(reason) or True
    )
    return calls


def _building_run(session, version: int, fingerprint: str | None = None, error: str | None = None, status="building"):
    from app.initiatives.i13.snapshot_store import kinds
    from app.models.i13_snapshot_store import I13SnapshotRecord, I13SnapshotRun

    session.add(
        I13SnapshotRun(
            version=version, status=status, fingerprint=fingerprint, schema_signature=kinds.signature(),
            reference_date=date.fromisoformat(AS_OF), started_at=datetime.now(timezone.utc), error=error,
        )
    )
    session.add(I13SnapshotRecord(version=version, kind="watch", seq=1, material="X", plant="1300", oar=True, payload="[]"))
    session.commit()


@needs_db
def test_a_page_request_never_starts_a_build(clean, started) -> None:
    from app.initiatives.i13.snapshot import SnapshotFailed
    from app.initiatives.i13.snapshot_store import lifecycle

    with pytest.raises(SnapshotFailed, match="no I13 snapshot has been built yet"):
        lifecycle.current(clean)
    assert started == [], "a request found no version and must not start one"


@needs_db
def test_the_lock_admits_one_build_and_frees_with_its_holder(clean) -> None:
    from app.initiatives.i13.snapshot_store import __main__ as cli
    from app.initiatives.i13.snapshot_store import build_lock

    lock = build_lock.BuildLock()
    assert lock.acquire()
    try:
        assert build_lock.held()
        assert cli.build("second build") == cli.EXIT_LOCKED
    finally:
        lock.release()
    assert not build_lock.held()


@needs_db
def test_start_up_never_cleans_up_a_running_build(clean, started) -> None:
    from app.initiatives.i13.snapshot_store import build_lock, lifecycle
    from app.models.i13_snapshot_store import I13SnapshotRun

    _building_run(clean, 1)
    other = build_lock.BuildLock()  # another process's live build
    assert other.acquire()
    try:
        lifecycle.start_up()
        clean.expire_all()
        assert clean.get(I13SnapshotRun, 1).status == "building", "a live build was abandoned"
        assert clean.execute(text("SELECT COUNT(*) FROM i13_snapshot_record WHERE version = 1")).scalar() == 1
        assert started == [], "start-up started a second build beside a live one"
    finally:
        other.release()

    # Its holder gone (the process died): start-up clears it and builds.
    lifecycle.start_up()
    clean.expire_all()
    assert clean.get(I13SnapshotRun, 1).status == "failed"
    assert clean.execute(text("SELECT COUNT(*) FROM i13_snapshot_record WHERE version = 1")).scalar() == 0
    assert started == ["start-up"]


@needs_db
def test_a_dead_build_reads_as_interrupted(clean) -> None:
    from app.initiatives.i13.snapshot_store import lifecycle

    _building_run(clean, 1)
    status = lifecycle.status()
    assert status["status"] == "failed"
    assert "interrupted" in status["last_error"]


@needs_db
def test_automatic_retries_stop_but_a_manual_refresh_runs(clean, monkeypatch) -> None:
    from app.initiatives.i13.snapshot import compute_fingerprint
    from app.initiatives.i13.snapshot_store import lifecycle

    fingerprint = compute_fingerprint(clean)
    for version in (1, 2, 3):
        _building_run(clean, version, fingerprint=fingerprint, error="boom", status="failed")

    launched: list[list[str]] = []

    class Done:
        def __init__(self, command, **_kwargs):
            launched.append(command)

        def wait(self):
            return 0

    monkeypatch.setattr(lifecycle.subprocess, "Popen", Done)
    lifecycle._launch_and_wait("data changed", None, manual=False)
    assert launched == [], "a fourth automatic attempt at the same data"
    lifecycle._launch_and_wait("manual refresh", None, manual=True)
    assert len(launched) == 1 and launched[0][-1] == "manual refresh"
    assert launched[0][1:4] == ["-m", "app.initiatives.i13.snapshot_store", "build"]


@needs_db
def test_the_build_runs_as_its_own_process(clean, settings) -> None:  # noqa: F811
    from app.initiatives.i13.snapshot_store import lifecycle
    from app.models.i13_snapshot_store import I13SnapshotRecord

    result = subprocess.run(
        [sys.executable, "-m", "app.initiatives.i13.snapshot_store", "build", "--reason", "process test"],
        cwd=lifecycle._APP_ROOT, env=os.environ.copy(), capture_output=True, text=True, timeout=600, check=False,
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    run = lifecycle.ready_run(clean)
    assert run is not None and run.reason == "process test"
    assert clean.execute(select(func.count()).select_from(I13SnapshotRecord)).scalar_one() > 0

    # And through the lifecycle, the way the API starts one.
    assert lifecycle.start_background_build("manual refresh", manual=True)
    lifecycle._state.thread.join(timeout=600)
    clean.expire_all()
    assert lifecycle.ready_run(clean).version == run.version + 1
    assert lifecycle._state.last_error is None


@needs_db
def test_it_builds_without_zmm065(clean, settings) -> None:  # noqa: F811
    """Last: drops the tenant's criticality table (its teardown drops the rest)."""
    from app.initiatives.i13.snapshot_store import builder, lifecycle
    from app.initiatives.i13.snapshot_store.reader import SqlSnapshot

    clean.execute(text("DROP TABLE IF EXISTS criticality_current"))
    clean.commit()
    builder.build(clean, reason="no zmm065", settings=settings)
    stored = SqlSnapshot(clean, lifecycle.ready_run(clean))
    candidates, total = stored.reclassification()
    assert total > 0
    assert all(c.critical_impact_indicator is None for c in candidates)
    watch, _ = stored.watch()
    assert watch

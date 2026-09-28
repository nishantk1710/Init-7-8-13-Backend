"""The I08 snapshot cache: built once, never holds a route indefinitely.

The cache tests replace the build itself, so they run anywhere. The last two
need a SQL Server (CI has one): the statement time limit, and the staged
universe query returning exactly what the direct one does.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.core.db import get_db, get_sessionmaker, statement_timeout
from app.initiatives.i8 import service
from app.initiatives.i8.material_number import series_like_patterns
from app.initiatives.i8.service import SnapshotBuilding, SnapshotFailed, get_snapshot
from app.initiatives.i8.universe import _render_candidates, fetch_universe_candidates
from app.main import app
from app.shared.sql_lists import like_any
from tests.i8_support import needs_db

DB = object()


class FakeBuild:
    """Stands in for build_snapshot: counts calls, can block on a gate, can fail."""

    def __init__(self, *, fail: bool = False, gate: threading.Event | None = None) -> None:
        self.fail = fail
        self.gate = gate
        self.calls = 0
        self.result = object()

    def __call__(self, db, cfg=None):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise RuntimeError("database unreachable\n[SQL: select secret_column from v_ekpo]")
        return self.result


class _Session:
    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fresh_cache(monkeypatch):
    monkeypatch.setattr(service, "get_sessionmaker", lambda: _Session)
    service.reset_snapshot()
    yield
    service.reset_snapshot()


def _install(monkeypatch, build: FakeBuild) -> FakeBuild:
    monkeypatch.setattr(service, "build_snapshot", build)
    return build


def _start_blocked_build(monkeypatch) -> tuple[FakeBuild, threading.Event]:
    gate = threading.Event()
    build = _install(monkeypatch, FakeBuild(gate=gate))
    assert service.start_background_build("test")
    deadline = time.monotonic() + 2
    while build.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert build.calls == 1, "the background build never started"
    return build, gate


def _finish(gate: threading.Event) -> None:
    gate.set()
    service._state.thread.join(5)


class TestTheCache:
    def test_builds_once_then_serves_the_cached_snapshot(self, monkeypatch) -> None:
        build = _install(monkeypatch, FakeBuild())
        assert get_snapshot(DB) is build.result
        assert get_snapshot(DB) is build.result
        assert build.calls == 1

    def test_a_bounded_caller_is_told_building_instead_of_being_held(self, monkeypatch) -> None:
        build, gate = _start_blocked_build(monkeypatch)
        try:
            started = time.monotonic()
            with pytest.raises(SnapshotBuilding) as building:
                get_snapshot(DB, wait_seconds=0.05)
            assert time.monotonic() - started < 1
            assert building.value.started_at is not None
            assert service.start_background_build("again") is False
        finally:
            _finish(gate)
        assert get_snapshot(DB, wait_seconds=0) is build.result
        assert build.calls == 1

    def test_an_unbounded_caller_waits_for_the_build_in_progress(self, monkeypatch) -> None:
        build, gate = _start_blocked_build(monkeypatch)
        threading.Timer(0.1, gate.set).start()
        assert get_snapshot(DB) is build.result
        service._state.thread.join(5)
        assert build.calls == 1

    def test_a_failure_is_reported_and_not_retried_by_every_caller(self, monkeypatch) -> None:
        build = _install(monkeypatch, FakeBuild(fail=True))
        with pytest.raises(SnapshotFailed) as failed:
            get_snapshot(DB)
        assert str(failed.value) == "RuntimeError: database unreachable"
        with pytest.raises(SnapshotFailed):
            get_snapshot(DB)
        assert build.calls == 1

        monkeypatch.setattr(service, "_RETRY_AFTER_FAILURE_SECONDS", 0.0)
        build.fail = False
        assert get_snapshot(DB) is build.result
        assert build.calls == 2

    def test_a_failed_background_build_is_reported_to_the_next_caller(self, monkeypatch) -> None:
        _install(monkeypatch, FakeBuild(fail=True))
        assert service.start_background_build("test")
        service._state.thread.join(5)
        with pytest.raises(SnapshotFailed, match="database unreachable"):
            get_snapshot(DB, wait_seconds=0)


def _no_db():
    yield None


@pytest.fixture
def api(monkeypatch):
    app.dependency_overrides[get_db] = _no_db
    monkeypatch.setattr(get_settings(), "i8_snapshot_wait_seconds", 0)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


class TestTheRoutes:
    def test_answer_503_building_with_retry_after(self, api, monkeypatch) -> None:
        _build, gate = _start_blocked_build(monkeypatch)
        try:
            response = api.get("/api/i8/register")
        finally:
            _finish(gate)
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "10"
        assert response.json()["detail"]["status"] == "building"

    def test_answer_503_failed_without_leaking_sql(self, api, monkeypatch) -> None:
        _install(monkeypatch, FakeBuild(fail=True))
        response = api.get("/api/i8/snapshot")
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "60"
        detail = response.json()["detail"]
        assert detail["status"] == "failed"
        assert "database unreachable" in detail["message"]
        assert "secret_column" not in detail["message"]


@needs_db
def test_statement_timeout_cancels_an_overrunning_statement() -> None:
    db = get_sessionmaker()()
    try:
        started = time.monotonic()
        with pytest.raises(OperationalError, match="HYT00"):
            with statement_timeout(db, 1):
                db.execute(text("waitfor delay '00:00:05'"))
        assert time.monotonic() - started < 4
        # SQLAlchemy discards a timed-out connection, so the next one starts clean.
        db.rollback()
        assert db.connection().connection.dbapi_connection.timeout == 0
        assert db.execute(text("select 1")).scalar() == 1
    finally:
        db.close()


@needs_db
def test_statement_timeout_is_put_back_after_a_clean_block() -> None:
    db = get_sessionmaker()()
    try:
        dbapi_connection = db.connection().connection.dbapi_connection
        with statement_timeout(db, 7):
            assert dbapi_connection.timeout == 7
            db.execute(text("select 1"))
        assert dbapi_connection.timeout == 0
    finally:
        db.close()


_STAND_INS = {
    "v_mara": ("i8t_mara", "matnr nvarchar(40), maktx nvarchar(80), mtart nvarchar(8)"),
    "v_makt": ("i8t_makt", "matnr nvarchar(40), maktx nvarchar(80)"),
    "v_marc": (
        "i8t_marc",
        "matnr nvarchar(40), werks nvarchar(8), minbe decimal(18, 3), dismm nvarchar(4), plifz decimal(9, 0)",
    ),
    "v_mard": ("i8t_mard", "matnr nvarchar(40), werks nvarchar(8), labst decimal(18, 3)"),
    "v_ekpo": ("i8t_ekpo", "matnr nvarchar(40), werks nvarchar(8), txz01 nvarchar(80)"),
    "v_zmm065": ("i8t_zmm065", "matnr nvarchar(40), werks nvarchar(8), maktx nvarchar(80)"),
}
_ROWS = [
    "insert into i8t_mara values ('8000000001', 'MARA PUMP', 'ZREP')",
    "insert into i8t_makt values ('8000000001', 'MAKT PUMP')",
    "insert into i8t_marc values ('8000000001', '1400', 2, 'VB', 30)",
    "insert into i8t_mard values ('8000000001', '1400', 3), ('8000000001', '1400', 4),"
    " ('8000000001', '1500', 5), ('1234567890', '1400', 9)",
    "insert into i8t_ekpo values ('8000000001', '1400', 'PO PUMP'), ('8000000002', '1500', 'PO VALVE')",
    "insert into i8t_zmm065 values ('8000000001', '1400', 'ZMM PUMP')",
]


@needs_db
def test_the_staged_universe_query_returns_what_the_direct_one_does() -> None:
    """Against stand-in tables, created and rolled back in one transaction."""
    db = get_sessionmaker()()
    try:
        for table, columns in _STAND_INS.values():
            db.execute(text(f"create table {table} ({columns})"))
        for statement in _ROWS:
            db.execute(text(statement))
        sources = {view: table for view, (table, _columns) in _STAND_INS.items()}
        matnr_like, params = like_any("matnr", "pattern", series_like_patterns())

        def key(row):
            return (row["matnr"], row["werks"] or "")

        direct = sorted(
            (dict(r) for r in db.execute(text(_render_candidates(sources, matnr_like)), params).mappings()),
            key=key,
        )
        staged = sorted(
            (dict(r) for r in fetch_universe_candidates(db, matnr_like, params, sources=sources)),
            key=key,
        )
        again = fetch_universe_candidates(db, matnr_like, params, sources=sources)

        assert staged == direct
        assert len(again) == len(staged)
        assert [key(r) for r in staged] == [
            ("8000000001", "1400"),
            ("8000000001", "1500"),
            ("8000000002", "1500"),
        ]
        pump = staged[0]
        assert pump["maktx"] == "ZMM PUMP"
        assert pump["labst"] == 7
        assert pump["locations"] == 2
        assert pump["minbe"] == 2
        assert staged[2]["maktx"] == "PO VALVE"
        assert staged[2]["labst"] is None
    finally:
        db.rollback()
        db.close()

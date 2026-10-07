"""The I08 snapshot cache: built once, never holds a route indefinitely.

The cache tests replace the build itself, so they run anywhere. The last two
need a SQL Server (CI has one): the statement time limit, and the staged
universe query returning exactly what the direct one does.
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timezone
from types import SimpleNamespace

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
from app.shared.sql_lists import fetch_in_chunks, like_any
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


def _start_blocked_build(monkeypatch, *, fail: bool = False) -> tuple[FakeBuild, threading.Event]:
    gate = threading.Event()
    build = _install(monkeypatch, FakeBuild(fail=fail, gate=gate))
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

    def test_a_bounded_caller_never_builds_in_its_own_thread(self, monkeypatch) -> None:
        # Built inline, the first request after a failure was held for the whole
        # build; behind an I13 build it would be held for that one too.
        threads: list[str] = []
        build = _install(monkeypatch, FakeBuild())
        original = build.__call__

        def recording(db, cfg=None):
            threads.append(threading.current_thread().name)
            return original(db, cfg)

        monkeypatch.setattr(service, "build_snapshot", recording)
        assert get_snapshot(DB, wait_seconds=5) is build.result
        assert threads == ["i8-snapshot"]

    def test_a_bounded_retry_after_failure_also_runs_in_the_background(self, monkeypatch) -> None:
        build = _install(monkeypatch, FakeBuild(fail=True))
        with pytest.raises(SnapshotFailed):
            get_snapshot(DB, wait_seconds=5)
        monkeypatch.setattr(service, "_RETRY_AFTER_FAILURE_SECONDS", 0.0)
        build.fail = False
        assert get_snapshot(DB, wait_seconds=5) is build.result
        assert build.calls == 2


class TestTheSourceFingerprint:
    """A reload underneath a running process has to reach the screens.

    The snapshot used to be cached until the process restarted, on the stated
    assumption that its source was a frozen July extract. A CSV full pull
    replaces ``raw_<table>`` whole, so a pull that landed at 05:00 stayed
    invisible until somebody restarted the App Service -- and the register went
    on reporting the previous pull's figures with nothing on the page to say so.
    """

    @staticmethod
    def _snapshot_with(fingerprint: str) -> SimpleNamespace:
        return SimpleNamespace(source_fingerprint=fingerprint, source_loaded_at=None)

    def _cache(self, monkeypatch, fingerprint: str) -> FakeBuild:
        """A cached snapshot built from ``fingerprint``."""
        build = _install(monkeypatch, FakeBuild())
        build.result = self._snapshot_with(fingerprint)
        assert get_snapshot(DB) is build.result
        return build

    @staticmethod
    def _source(monkeypatch, fingerprint: str) -> None:
        """What the raw layer says it is now."""
        monkeypatch.setattr(
            service,
            "source_state",
            lambda db, *, reference_date: service.SourceState(fingerprint, None),
        )

    @staticmethod
    def _never(message: str):
        def _call(db, *, reference_date):
            raise AssertionError(message)

        return _call

    def test_an_unchanged_source_does_not_rebuild(self, monkeypatch) -> None:
        build = self._cache(monkeypatch, "same")
        self._source(monkeypatch, "same")
        assert service.check_source_fingerprint(DB, min_interval_seconds=0) is False
        assert build.calls == 1

    def test_a_reload_rebuilds(self, monkeypatch) -> None:
        build = self._cache(monkeypatch, "before the pull")
        self._source(monkeypatch, "after the pull")
        assert service.check_source_fingerprint(DB, min_interval_seconds=0) is True
        service._state.thread.join(5)
        assert build.calls == 2
        assert get_snapshot(DB, wait_seconds=0) is build.result

    def test_the_old_snapshot_keeps_serving_while_the_rebuild_runs(self, monkeypatch) -> None:
        """A rebuild must not turn every request into a 503 the way a first
        build does. There is a perfectly good answer cached; it is merely old."""
        build = self._cache(monkeypatch, "before")
        stale = build.result
        gate = threading.Event()
        build.gate = gate
        build.result = self._snapshot_with("after")
        self._source(monkeypatch, "after")

        assert service.check_source_fingerprint(DB, min_interval_seconds=0) is True
        try:
            assert get_snapshot(DB, wait_seconds=0) is stale
        finally:
            _finish(gate)
        assert get_snapshot(DB, wait_seconds=0) is build.result
        assert build.calls == 2

    def test_the_check_is_rate_limited(self, monkeypatch) -> None:
        build = self._cache(monkeypatch, "before")
        self._source(monkeypatch, "after")
        assert service.check_source_fingerprint(DB, min_interval_seconds=3600) is True
        service._state.thread.join(5)

        monkeypatch.setattr(
            service, "source_state", self._never("the source was queried inside the rate limit")
        )
        assert service.check_source_fingerprint(DB, min_interval_seconds=3600) is False
        assert build.calls == 2

    def test_there_is_nothing_to_check_before_the_first_build(self, monkeypatch) -> None:
        monkeypatch.setattr(
            service, "source_state", self._never("the source was queried with nothing to compare")
        )
        assert service.check_source_fingerprint(DB, min_interval_seconds=0) is False


class TestWhatTheFingerprintCovers:
    """``source_state`` itself, without a database."""

    class _Db:
        def __init__(self, rows: list[tuple]) -> None:
            self.rows = rows

        def execute(self, _statement):
            return self

        def all(self) -> list[tuple]:
            return self.rows

    def test_a_new_day_is_a_new_fingerprint(self) -> None:
        """With I8_REFERENCE_DATE unset the snapshot measures aging as of the
        day it was built. One that survives midnight reports yesterday's
        overdue counts under today's heading unless the date is in here."""
        db = self._Db([])
        monday = service.source_state(db, reference_date=date(2026, 10, 6))
        tuesday = service.source_state(db, reference_date=date(2026, 10, 7))
        assert monday.fingerprint != tuesday.fingerprint

    def test_a_reload_of_any_one_table_changes_it(self) -> None:
        before = service.source_state(
            self._Db([("raw_mara", 1, None), ("raw_ekpo", 7, None)]),
            reference_date=date(2026, 10, 7),
        )
        after = service.source_state(
            self._Db([("raw_mara", 1, None), ("raw_ekpo", 8, None)]),
            reference_date=date(2026, 10, 7),
        )
        assert before.fingerprint != after.fingerprint

    def test_loaded_at_is_the_newest_load_and_survives_a_table_with_none(self) -> None:
        """A table that has never loaded reports None, and mixing None into
        ``max()`` is a TypeError -- a crash in the watcher, not a wrong date."""
        state = service.source_state(
            self._Db(
                [
                    ("raw_ekpo", 2, datetime(2026, 10, 7, 5, 1, tzinfo=timezone.utc)),
                    ("raw_mara", 1, datetime(2026, 10, 7, 5, 0, tzinfo=timezone.utc)),
                    ("raw_zmm065_gb", 3, None),
                ]
            ),
            reference_date=date(2026, 10, 7),
        )
        assert state.loaded_at == datetime(2026, 10, 7, 5, 1, tzinfo=timezone.utc)

    def test_nothing_loaded_is_reported_as_nothing(self) -> None:
        state = service.source_state(self._Db([]), reference_date=date(2026, 10, 7))
        assert state.loaded_at is None

    def test_every_table_the_views_read_is_covered(self) -> None:
        """A table missing from SOURCE_TABLES is a reload I08 never notices, so
        the list is checked against the views rather than trusted. v_zmm065 is
        the union that makes this a mapping and not a rename."""
        from app.initiatives.i8.views import VIEWS

        expected = {f"raw_{view.removeprefix('v_')}" for view in VIEWS} - {"raw_zmm065"}
        expected |= {"raw_zmm065_bmm", "raw_zmm065_gb"}
        assert set(service.SOURCE_TABLES) == expected


class TestOneBuildAtATime:
    """I08 and I13 never build side by side (app/shared/snapshot_builds.py)."""

    def test_a_second_build_waits_for_the_first(self) -> None:
        from app.shared.snapshot_builds import exclusive_build

        order: list[str] = []
        first_in = threading.Event()
        release = threading.Event()

        def first() -> None:
            with exclusive_build("I08"):
                order.append("I08 start")
                first_in.set()
                release.wait(5)
                order.append("I08 end")

        def second() -> None:
            with exclusive_build("I13"):
                order.append("I13 start")

        a = threading.Thread(target=first)
        a.start()
        assert first_in.wait(5)
        b = threading.Thread(target=second)
        b.start()
        time.sleep(0.1)
        assert order == ["I08 start"], "the second build started while the first held the slot"
        release.set()
        a.join(5)
        b.join(5)
        assert order == ["I08 start", "I08 end", "I13 start"]

    def test_the_i13_start_up_build_waits_for_the_i08_one(self, monkeypatch) -> None:
        from app.initiatives.i13 import snapshot as i13_snapshot

        started = threading.Event()
        monkeypatch.setattr(i13_snapshot, "_build_and_swap", lambda reason: started.set())
        gate = threading.Event()
        i08 = threading.Thread(target=gate.wait, args=(5,))
        i08.start()
        try:
            assert i13_snapshot.start_background_build("start-up", after=i08)
            assert not started.wait(0.2), "I13 started before the I08 build finished"
        finally:
            gate.set()
        assert started.wait(5)
        i13_snapshot._state.thread.join(5)
        i13_snapshot.reset_i13_snapshot()


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
        # Gated, not a bare FakeBuild(fail=True): an ungated fake has no work to
        # do before it fails, so it can finish -- and flip _state.last_error --
        # before the request below ever checks in. The "building" assertion was
        # racing the background thread and losing on a loaded runner (observed
        # in CI), while reliably winning on a quiet dev machine. See
        # test_answer_503_building_with_retry_after for the same gated pattern.
        _build, gate = _start_blocked_build(monkeypatch, fail=True)
        try:
            # The first request starts the build and answers "building"; once
            # that build has failed, the next one says so.
            assert api.get("/api/i8/snapshot").json()["detail"]["status"] == "building"
        finally:
            _finish(gate)
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


_MSEG_COLUMNS = (
    "ebeln nvarchar(40), ebelp nvarchar(10), budat date, menge decimal(18, 3),"
    " bwart nvarchar(4), sobkz nvarchar(2)"
)
_MSEG_ROWS = (
    # Two dispatches on one line, and the plant-side twin each 541 posts.
    "insert into i8t_mseg values"
    " ('4100000372', '10', '2026-07-01', 1, '541', 'O'),"
    " ('4100000372', '10', '2026-07-09', 2, '541', 'O'),"
    " ('4100000372', '10', '2026-07-01', 1, '541', null),"
    # A second line on the same PO, a receipt, a dispatch with no PO item,
    # and a dispatch on a PO the register did not ask for.
    " ('4100000372', '20', '2026-07-03', 4, '541', 'O'),"
    " ('4100000372', '10', '2026-07-20', 3, '101', null),"
    " ('4100000410', null, '2026-07-05', 5, '541', 'O'),"
    " ('4100009999', '10', '2026-07-06', 6, '541', 'O')"
)


@needs_db
def test_the_staged_dispatch_query_returns_what_the_direct_one_does() -> None:
    """Against a stand-in for v_mseg, created and rolled back in one transaction."""
    from app.initiatives.i8.register import _DISPATCH_SQL, fetch_dispatches

    db = get_sessionmaker()()
    try:
        db.execute(text(f"create table i8t_mseg ({_MSEG_COLUMNS})"))
        db.execute(text(_MSEG_ROWS))
        documents = ["4100000372", "4100000410"]
        params = {"dispatch": "541", "vendor_stock": "O"}

        def key(row):
            return (row["ebeln"], row["ebelp"])

        direct = sorted(
            (
                dict(r)
                for r in fetch_in_chunks(
                    db, _DISPATCH_SQL.replace("v_mseg", "i8t_mseg"), "documents", documents, params
                )
            ),
            key=key,
        )
        staged = sorted(
            (dict(r) for r in fetch_dispatches(db, documents, params, source="i8t_mseg")),
            key=key,
        )
        again = fetch_dispatches(db, documents, params, source="i8t_mseg")

        assert staged == direct
        assert len(again) == len(staged)
        assert [key(r) for r in staged] == [("4100000372", "10"), ("4100000372", "20")]
        line = staged[0]
        assert line["qty"] == 3
        assert line["postings"] == 2
        assert str(line["first_dispatch"]) == "2026-07-01"
        assert str(line["last_dispatch"]) == "2026-07-09"
    finally:
        db.rollback()
        db.close()

"""The stored I08 snapshot (``I8_SNAPSHOT_STORE=sql``) without a database.

The codec gives back exactly what it was given, and the API side serves a
stored version instead of building, builds in a process only when it must, and
stops retrying a build that keeps failing. The round trip through the real
table, and a build process over a seeded tenant, are in
``test_i8_staged_build.py`` (they need SQL Server).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.initiatives.i8 import service
from app.initiatives.i8.acquisitions import NewAcquisition
from app.initiatives.i8.register import RegisterStats
from app.initiatives.i8.requisitions import RepairRequest
from app.initiatives.i8.snapshot_store import codec, lifecycle, store
from app.initiatives.i8.universe import UniverseStats
from tests.assistant.conftest import repair_line, universe_row


def _snapshot(**overrides) -> service.Snapshot:
    fields = dict(
        reference_date=date(2026, 10, 1),
        built_at=datetime(2026, 10, 1, 6, 0, tzinfo=timezone.utc),
        build_seconds=12.5,
        lines=(
            repair_line(),
            repair_line(item="00020", net_price=Decimal("1.000"), dispatched_at=date(2026, 9, 1)),
        ),
        register_stats=RegisterStats(
            total_lines=2,
            open_lines=2,
            candidates_scanned=805336,
            excluded_delivery_completed_lines=644,
            open_repair_requests=1,
            requisitions_excluded_live_po=2296,
        ),
        universe=(universe_row(), universe_row(plant="1500", reorder_point=Decimal("2.5"), criticality=None)),
        universe_stats=UniverseStats(rows=2, materials=1, plants=2, by_source={"mara": 1, "ekpo": 1}),
        acquisitions=(
            NewAcquisition("4500000010", "10", "8000000003", "1300", "NEW VALVE", Decimal("1"), date(2026, 9, 1)),
        ),
        source_fingerprint="raw_ekpo:7:2026-10-01 05:00:00|date:2026-10-01",
        source_loaded_at=datetime(2026, 10, 1, 5, 0, tzinfo=timezone.utc),
        repair_requests=(
            RepairRequest(
                purchase_requisition="2000029113",
                item="10",
                material_id="8000003325",
                plant="1300",
                description="BEARING ASSEMBLY",
                quantity=Decimal("1.000"),
                unit="EA",
                requested_on=date(2026, 9, 1),
                need_date=None,
                department="10305",
                requisition_status="N",
                document_type="ZMNP",
                repair_order="940000004981",
                reservation="1916164",
                days_open=30,
                aging_bucket="16-30",
            ),
        ),
    )
    fields.update(overrides)
    return service.Snapshot(**fields)


# --- the codec -------------------------------------------------------------------


def test_the_codec_round_trips_a_snapshot_exactly() -> None:
    snapshot = _snapshot()
    back = codec.decode(codec.encode(snapshot))

    assert back == snapshot
    assert isinstance(back.lines, tuple) and isinstance(back.lines[0], type(snapshot.lines[0]))
    # Digits kept: 1.000 is not 1.
    assert str(back.lines[1].net_price) == "1.000"
    assert back.universe_stats.by_source == {"mara": 1, "ekpo": 1}
    assert back.line(*snapshot.lines[0].key) == snapshot.lines[0]
    # The open repair PRs (codec 4, 27-Oct-2026): kept apart from the PO lines,
    # digits and all, and found by their own key.
    assert isinstance(back.repair_requests, tuple)
    assert isinstance(back.repair_requests[0], RepairRequest)
    assert str(back.repair_requests[0].quantity) == "1.000"
    assert back.request("2000029113", "10") == snapshot.repair_requests[0]
    assert back.request("2000029113", "20") is None
    assert back.register_stats.requisitions_excluded_live_po == 2296


def test_a_snapshot_without_repair_requests_still_round_trips() -> None:
    """The field defaults, so a snapshot built with none is still exact."""
    snapshot = _snapshot(repair_requests=())
    back = codec.decode(codec.encode(snapshot))
    assert back == snapshot
    assert back.request("2000029113", "10") is None


def test_the_codec_constructs_only_i08_classes() -> None:
    import gzip
    import json

    payload = codec.encode({"x": 1})
    forged = gzip.compress(json.dumps({"$t": "os:system", "f": {"command": "echo"}}).encode())
    with pytest.raises(ValueError, match="refusing"):
        codec.decode(forged)
    assert codec.decode(payload) == {"x": 1}


# --- load or build -----------------------------------------------------------------


class Calls:
    def __init__(self, *, code: int = 0, stored=None, failures: int = 0, error: str | None = None) -> None:
        self.code = code
        self.stored = stored
        self.failures = failures
        self.error = error
        self.builds: list[tuple[str, bool]] = []


@pytest.fixture
def fake_store(monkeypatch):
    def install(calls: Calls) -> Calls:
        def run_build(reason, *, force=False):
            calls.builds.append((reason, force))
            if calls.code == 0 and calls.stored is None:
                calls.stored = SimpleNamespace(version=1, built_at=None, payload=codec.encode(_snapshot()))
            return calls.code

        monkeypatch.setattr(lifecycle, "run_build", run_build)
        monkeypatch.setattr(store, "latest_ready", lambda db: calls.stored)
        monkeypatch.setattr(store, "recent_failures", lambda db: calls.failures)
        monkeypatch.setattr(
            store, "latest",
            lambda db: SimpleNamespace(status=store.FAILED, error=calls.error) if calls.error else None,
        )
        return calls

    return install


class _Db:
    def rollback(self) -> None:
        pass


def test_a_stored_version_is_served_without_building(fake_store) -> None:
    calls = fake_store(Calls(stored=SimpleNamespace(version=7, built_at=None, payload=codec.encode(_snapshot()))))

    snapshot = lifecycle.load_or_build(_Db(), "start-up", rebuild=False)

    assert snapshot == _snapshot()
    assert calls.builds == []


def test_nothing_stored_builds_in_a_process_then_loads(fake_store) -> None:
    calls = fake_store(Calls())

    snapshot = lifecycle.load_or_build(_Db(), "first request", rebuild=False)

    assert calls.builds == [("first request", False)]
    assert snapshot == _snapshot()


def test_a_rebuild_builds_even_with_a_stored_version(fake_store) -> None:
    calls = fake_store(Calls(stored=SimpleNamespace(version=7, built_at=None, payload=codec.encode(_snapshot()))))

    lifecycle.load_or_build(_Db(), "source data changed", rebuild=True)

    assert calls.builds == [("source data changed", False)]


def test_a_failed_build_reports_the_run_error(fake_store) -> None:
    fake_store(Calls(code=1, error="OperationalError: Query timeout expired"))

    with pytest.raises(RuntimeError, match="Query timeout expired"):
        lifecycle.load_or_build(_Db(), "first request", rebuild=False)


def test_repeated_failures_stop_automatic_builds_but_not_a_manual_one(fake_store) -> None:
    calls = fake_store(Calls(failures=get_settings().i8_build_max_attempts))

    with pytest.raises(RuntimeError, match="not retrying automatically"):
        lifecycle.load_or_build(_Db(), "source data changed", rebuild=True)
    assert calls.builds == []

    lifecycle.load_or_build(_Db(), "manual refresh", rebuild=True)
    assert calls.builds == [("manual refresh", True)]


def test_with_the_store_on_the_worker_never_builds_itself(monkeypatch) -> None:
    monkeypatch.setenv("I8_SNAPSHOT_STORE", "sql")
    get_settings.cache_clear()
    try:
        def no_build(*args, **kwargs):
            raise AssertionError("built in the web worker with the SQL store on")

        monkeypatch.setattr(service, "build_snapshot", no_build)
        loaded = _snapshot()
        seen = []
        monkeypatch.setattr(
            lifecycle, "load_or_build", lambda db, reason, *, rebuild: seen.append((reason, rebuild)) or loaded
        )
        service.reset_snapshot()

        assert service._build_locked(_Db(), None, "start-up") is loaded
        assert service._build_locked(_Db(), None, "source data changed", rebuild=True) is loaded
        assert seen == [("start-up", False), ("source data changed", True)]
    finally:
        service.reset_snapshot()
        get_settings.cache_clear()


def test_the_watcher_takes_a_newer_stored_version_without_building(monkeypatch) -> None:
    """A build run by hand, or by the other container, reaches this process."""
    current = _snapshot()
    newer = _snapshot(built_at=datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc), build_seconds=3.0)
    monkeypatch.setattr(lifecycle, "enabled", lambda: True)
    monkeypatch.setattr(lifecycle, "newer_stored", lambda db, built_at: newer if built_at < newer.built_at else None)
    monkeypatch.setattr(
        service, "source_state",
        lambda db, reference_date: SimpleNamespace(fingerprint=current.source_fingerprint, loaded_at=None),
    )
    monkeypatch.setattr(service, "start_background_build", lambda *a, **k: pytest.fail("rebuilt instead of loading"))
    service.reset_snapshot()
    try:
        with service._state_lock:
            service._state.snapshot = current

        assert service.check_source_fingerprint(_Db(), min_interval_seconds=0) is False
        assert service._state.snapshot is newer
    finally:
        service.reset_snapshot()

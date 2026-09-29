"""The two timers: what a full-refresh cycle does, in what order, and when.

Everything the cycle calls is stubbed; the sequencing is what is tested.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.ingest import scheduler


class _Fake:
    def __init__(self, monkeypatch, *, pull_ok=True, load_ok=True, fetch_ok=True, merge_ok=True):
        from app.ingest import csv_load, csv_pull, fetch, load
        from app.ingest.csv_load import CsvLoadResult, STATUS_SUCCEEDED as LOAD_OK, STATUS_FAILED as LOAD_BAD
        from app.ingest.csv_pull import PullResult
        from app.ingest.fetch import FetchResult
        from app.ingest.load import LoadResult, STATUS_SUCCEEDED, STATUS_FAILED
        from app.models.csv_extract import STATUS_COMPLETE, STATUS_TIMEOUT

        self.log: list[str] = []

        def pull_all(**kw):
            self.log.append("csv pull")
            return [PullResult("MARA", "R1", STATUS_COMPLETE if pull_ok else STATUS_TIMEOUT, received_rows=2040),
                    PullResult("EKKO", "R2", STATUS_TIMEOUT, error="no chunk arrived")]

        def load_all(**kw):
            self.log.append("csv load")
            return [CsvLoadResult("MARA", "raw_mara", LOAD_OK if load_ok else LOAD_BAD, rows=2040,
                                  error=None if load_ok else "disk")]

        def fetch_set(spec, **kw):
            self.log.append(f"fetch {spec.name} {kw.get('mode')} advance={kw.get('advance_watermark')}")
            r = FetchResult(entity_set=spec.name, prefix="p", mode="full", rows=2183)
            if not fetch_ok:
                r.error = "boom"
            return r

        def load_set(spec, **kw):
            self.log.append(f"load {spec.name}")
            return LoadResult(spec.name, spec.raw_table, STATUS_SUCCEEDED if merge_ok else STATUS_FAILED,
                              rows=2183, raw="raw_marc: 2183 updated, 0 inserted" if merge_ok else None,
                              error=None if merge_ok else "merge refused")

        monkeypatch.setattr(csv_pull, "pull_all", pull_all)
        monkeypatch.setattr(csv_load, "load_all", load_all)
        monkeypatch.setattr(fetch, "fetch_set", fetch_set)
        monkeypatch.setattr(load, "load_set", load_set)


def test_the_cycle_runs_sweep_then_load_then_enrichment(monkeypatch) -> None:
    fake = _Fake(monkeypatch)

    summary = scheduler.run_full_refresh_cycle()

    assert fake.log == ["csv pull", "csv load", "fetch MaterialPlantSet full advance=False", "load MaterialPlantSet"]
    assert summary["pulled"] == 1 and summary["loaded"] == 1 and summary["enriched"] == 1
    assert summary["rows"] == 2040
    # EKKO's timeout is reported, not hidden, and does not stop the rest.
    assert summary["failed"] == 1 and any("EKKO" in e for e in summary["errors"])


def test_a_failed_enrichment_is_counted_not_raised(monkeypatch) -> None:
    _Fake(monkeypatch, merge_ok=False)

    summary = scheduler.run_full_refresh_cycle()

    assert summary["enriched"] == 0
    assert any("MaterialPlantSet" in e and "merge refused" in e for e in summary["errors"])


def test_enrichment_never_advances_a_watermark(monkeypatch) -> None:
    """A full read is not a measured delta position; the CSV load owns the mark."""
    fake = _Fake(monkeypatch)
    scheduler.run_full_refresh_cycle()
    assert "advance=False" in fake.log[2]


class TestSecondsUntil:
    def test_later_today(self) -> None:
        now = datetime(2026, 9, 28, 0, 30, tzinfo=timezone.utc)
        assert scheduler.seconds_until(1, now) == 30 * 60

    def test_already_past_means_tomorrow(self) -> None:
        now = datetime(2026, 9, 28, 1, 0, 1, tzinfo=timezone.utc)
        assert scheduler.seconds_until(1, now) == pytest.approx(24 * 3600 - 1)

    def test_hour_wraps(self) -> None:
        now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
        assert scheduler.seconds_until(25, now) == 13 * 3600


class TestStart:
    def _settings(self, monkeypatch, **overrides):
        from app.core.config import Settings
        base = dict(database_url="mssql+pyodbc://x", storage_url="abfss://y", cpi_base_url="", cpi_token_url="",
                    cpi_client_id="", cpi_client_secret="")
        base.update(overrides)
        s = Settings(_env_file=None, **base)
        monkeypatch.setattr(scheduler, "get_settings", lambda: s)
        return s

    def test_nothing_enabled_starts_nothing(self, monkeypatch) -> None:
        self._settings(monkeypatch)
        assert scheduler.start(None) is None

    def test_full_refresh_alone_starts_a_task(self, monkeypatch) -> None:
        import asyncio

        self._settings(monkeypatch, full_refresh_enabled=True)

        async def run():
            task = scheduler.start(None)
            assert task is not None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run())

    def test_missing_database_refuses_to_start(self, monkeypatch) -> None:
        self._settings(monkeypatch, full_refresh_enabled=True, database_url="")
        assert scheduler.start(None) is None

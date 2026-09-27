"""The workbook fallback: scope, the guard against overwriting live data,
the upload mapping, and SAP-first.

The seed loader, the CSV route and the database are stubbed. What is under
test is the decisions this command makes around them -- chiefly that it can
never put a July workbook over a table SAP has actually delivered.
"""

from __future__ import annotations

import contextlib
import io
from datetime import datetime, timezone

import pytest

from app.ingest import fallback
from app.seed.loader import STATUS_FAILED, STATUS_SUCCEEDED, TableResult


class _Live:
    request_id = "FEKKO90000000"
    received_rows = 3140
    loaded_at = datetime(2026, 9, 27, 7, 0, tzinfo=timezone.utc)


@pytest.fixture
def seed_calls(monkeypatch):
    """Record every seed load; answer success."""
    calls: list[tuple[str, bool]] = []

    def load_table(spec, *, force=False):
        calls.append((spec.table, force))
        return TableResult(spec.table, STATUS_SUCCEEDED, rows=10)

    monkeypatch.setattr(fallback.seed_loader, "load_table", load_table)
    monkeypatch.setattr(fallback, "live_data_loaded", lambda sap_table: None)
    return calls


# --- Scope ------------------------------------------------------------------


class TestScope:
    def test_defaults_to_exactly_the_five(self) -> None:
        assert [s.table for s in fallback.specs()] == list(fallback.FALLBACK_TABLES)

    def test_a_live_table_is_refused_by_name(self) -> None:
        """EKPO has a working CSV route; a workbook over it is a regression."""
        with pytest.raises(ValueError, match="ekpo: not a fallback table"):
            fallback.specs(["ekko", "ekpo"])

    def test_names_are_case_insensitive(self) -> None:
        assert [s.table for s in fallback.specs(["EKKO"])] == ["ekko"]


# --- The guard --------------------------------------------------------------


class TestGuard:
    def test_loads_when_no_live_data_was_ever_loaded(self, seed_calls) -> None:
        outcomes = fallback.load(["ekko"])

        assert seed_calls == [("ekko", True)]
        assert outcomes[0].source == fallback.SOURCE_WORKBOOK
        assert outcomes[0].ok

    def test_refuses_over_live_data(self, seed_calls, monkeypatch) -> None:
        """The one thing this command must never do."""
        monkeypatch.setattr(fallback, "live_data_loaded", lambda sap_table: _Live())

        outcomes = fallback.load(["ekko"])

        assert seed_calls == []
        assert outcomes[0].status == fallback.STATUS_REFUSED
        assert "FEKKO90000000" in outcomes[0].detail
        assert not outcomes[0].ok

    def test_force_overrides_the_guard(self, seed_calls, monkeypatch) -> None:
        monkeypatch.setattr(fallback, "live_data_loaded", lambda sap_table: _Live())

        outcomes = fallback.load(["ekko"], force=True)

        assert seed_calls == [("ekko", True)]
        assert outcomes[0].ok

    def test_the_guard_asks_about_the_sap_table_not_the_seed_name(self, monkeypatch) -> None:
        asked: list[str] = []
        monkeypatch.setattr(fallback, "live_data_loaded", lambda t: asked.append(t) or None)
        monkeypatch.setattr(
            fallback.seed_loader, "load_table",
            lambda spec, force=False: TableResult(spec.table, STATUS_SUCCEEDED),
        )

        fallback.load(["ekko", "zmm065_gb"])

        assert asked == ["EKKO", "ZMM065"]

    def test_one_failure_does_not_stop_the_rest(self, monkeypatch) -> None:
        monkeypatch.setattr(fallback, "live_data_loaded", lambda sap_table: None)

        def load_table(spec, *, force=False):
            if spec.table == "ekko":
                return TableResult(spec.table, STATUS_FAILED, error="workbook missing")
            return TableResult(spec.table, STATUS_SUCCEEDED, rows=5)

        monkeypatch.setattr(fallback.seed_loader, "load_table", load_table)

        outcomes = fallback.load(["ekko", "eket"])

        assert [o.status for o in outcomes] == [STATUS_FAILED, STATUS_SUCCEEDED]
        assert outcomes[0].detail == "workbook missing"


# --- SAP first --------------------------------------------------------------


class _CsvRoute:
    """Stand-in for csv_pull.pull_one / csv_load.load_table."""

    def __init__(self, delivers: bool, loads: bool = True) -> None:
        self.delivers, self.loads = delivers, loads
        self.fired: list[str] = []

    def pull_one(self, sap_table, **kw):
        self.fired.append(sap_table)
        from app.ingest.csv_pull import PullResult
        from app.models.csv_extract import STATUS_COMPLETE, STATUS_TIMEOUT
        return PullResult(sap_table, "R", STATUS_COMPLETE if self.delivers else STATUS_TIMEOUT,
                          received_rows=3140 if self.delivers else 0,
                          error=None if self.delivers else "no chunk arrived in 15 minutes")

    def load_table(self, sap_table, **kw):
        from app.ingest.csv_load import CsvLoadResult, STATUS_SUCCEEDED as OK, STATUS_FAILED as BAD
        return CsvLoadResult(sap_table, f"raw_{sap_table.lower()}", OK if self.loads else BAD,
                             rows=3140 if self.loads else 0, error=None if self.loads else "disk")


@pytest.fixture
def csv_route(monkeypatch):
    def install(delivers: bool, loads: bool = True) -> _CsvRoute:
        route = _CsvRoute(delivers, loads)
        from app.ingest import csv_load, csv_pull
        monkeypatch.setattr(csv_pull, "pull_one", route.pull_one)
        monkeypatch.setattr(csv_load, "load_table", route.load_table)
        return route
    return install


class TestTrySap:
    def test_sap_delivering_means_no_workbook(self, seed_calls, csv_route) -> None:
        route = csv_route(delivers=True)

        outcomes = fallback.load(["ekko"], try_sap=True)

        assert route.fired == ["EKKO"]
        assert seed_calls == []
        assert outcomes[0].source == fallback.SOURCE_SAP
        assert outcomes[0].rows == 3140

    def test_sap_silent_means_the_workbook(self, seed_calls, csv_route) -> None:
        route = csv_route(delivers=False)

        outcomes = fallback.load(["ekko"], try_sap=True)

        assert route.fired == ["EKKO"]
        assert seed_calls == [("ekko", True)]
        assert outcomes[0].source == fallback.SOURCE_WORKBOOK

    def test_sap_delivering_but_not_loading_means_the_workbook(self, seed_calls, csv_route) -> None:
        csv_route(delivers=True, loads=False)

        outcomes = fallback.load(["eket"], try_sap=True)

        assert seed_calls == [("eket", True)]
        assert outcomes[0].source == fallback.SOURCE_WORKBOOK

    def test_reports_are_never_asked_of_sap(self, seed_calls, csv_route) -> None:
        route = csv_route(delivers=True)

        fallback.load(["zmm065_gb", "gr_30day"], try_sap=True)

        assert route.fired == []
        assert [t for t, _ in seed_calls] == ["zmm065_gb", "gr_30day"]

    def test_the_window_asked_of_sap_is_everything(self, seed_calls, monkeypatch) -> None:
        seen: dict = {}
        from app.ingest import csv_pull
        from app.ingest.csv_pull import PullResult
        from app.models.csv_extract import STATUS_TIMEOUT

        def pull_one(sap_table, **kw):
            seen.update(kw)
            return PullResult(sap_table, "R", STATUS_TIMEOUT)

        monkeypatch.setattr(csv_pull, "pull_one", pull_one)

        fallback.load(["ekko"], try_sap=True)

        assert seen["from_date"] == "19000101"
        assert seen["to_date"] > "20270101"


# --- Upload -----------------------------------------------------------------


class _Sink:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    @contextlib.contextmanager
    def open_write(self, key: str):
        buffer = io.BytesIO()
        yield buffer
        self.objects[key] = buffer.getvalue()


class TestUpload:
    def test_files_land_at_the_manifest_keys_by_name(self, tmp_path) -> None:
        (tmp_path / "EKKO.XLSX").write_bytes(b"x" * 10)
        (tmp_path / "30 Day GR Report.xlsx").write_bytes(b"y" * 3)
        sink = _Sink()

        landed = fallback.upload(
            [str(tmp_path / "EKKO.XLSX"), str(tmp_path / "30 Day GR Report.xlsx")], sink
        )

        assert {key for _, key, _ in landed} == {
            "KPI 02 Data Extract/Tables/EKKO.XLSX",
            "Resources Shared - Rohit/30 Day GR Report.xlsx",
        }
        assert sink.objects["KPI 02 Data Extract/Tables/EKKO.XLSX"] == b"x" * 10

    def test_name_matching_ignores_case(self, tmp_path) -> None:
        (tmp_path / "eket.xlsx").write_bytes(b"z")
        sink = _Sink()

        fallback.upload([str(tmp_path / "eket.xlsx")], sink)

        assert "KPI 02 Data Extract/Tables/EKET.XLSX" in sink.objects

    def test_a_workbook_outside_the_five_is_refused(self, tmp_path) -> None:
        """Mara.XLSX has a live route; landing it would only invite a seed
        over live data."""
        (tmp_path / "Mara.XLSX").write_bytes(b"m")

        with pytest.raises(ValueError, match="not one of the fallback workbooks"):
            fallback.upload([str(tmp_path / "Mara.XLSX")], _Sink())

    def test_a_missing_local_file_is_named(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError):
            fallback.upload([str(tmp_path / "EKKO.XLSX")], _Sink())


# --- The live routes are untouched ------------------------------------------


def test_the_csv_sweep_still_leaves_ekko_and_eket_out() -> None:
    """--try-sap here is the only place that asks SAP for them again."""
    from app.ingest.csv_tables import csv_table

    assert csv_table("EKKO").blocked
    assert csv_table("EKET").blocked

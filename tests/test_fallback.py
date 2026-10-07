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
    monkeypatch.setattr(fallback, "seed_watermark", lambda table: None)
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


# --- The source folder ------------------------------------------------------

# The five workbook names as the manifest spells them, and a case-mangled copy
# of each: matching must not depend on how someone saved the file.
FIVE = {
    "ekko": "EKKO.XLSX",
    "eket": "EKET.XLSX",
    "zmm065_gb": "GB-ZMM065 - July 2026.XLSX",
    "zmm065_bmm": "BMM-ZMM065_Aging_Jul 26.xlsx",
    "gr_30day": "30 Day GR Report.xlsx",
}


def _folder(tmp_path, *names: str):
    for name in names:
        (tmp_path / name).write_bytes(name.encode())
    return tmp_path


class _RecordingSink(_Sink):
    """A sink that also records the order of events, shared with the seed stub."""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    @contextlib.contextmanager
    def open_write(self, key: str):
        with super().open_write(key) as buffer:
            yield buffer
        self.events.append(f"upload:{key}")


class TestFolderScan:
    def test_finds_the_five_by_name_whatever_the_case(self, tmp_path) -> None:
        folder = _folder(tmp_path, *(name.swapcase() for name in FIVE.values()))

        entries = fallback.scan(folder)

        assert {e.table: e.status for e in entries} == {t: fallback.FOUND for t in FIVE}
        assert all(e.local is not None for e in entries)

    def test_an_unknown_file_is_refused_and_named(self, tmp_path) -> None:
        folder = _folder(tmp_path, "EKKO.XLSX", "Mara.XLSX")
        sink = _Sink()

        entries = fallback.upload_folder(folder, sink)

        refused = [e for e in entries if e.status == fallback.REFUSED]
        assert [e.name for e in refused] == ["Mara.XLSX"]
        assert refused[0].table is None
        assert not any("Mara" in key for key in sink.objects)
        assert "KPI 02 Data Extract/Tables/EKKO.XLSX" in sink.objects

    def test_the_committed_readme_is_neither_uploaded_nor_refused(self, tmp_path) -> None:
        folder = _folder(tmp_path, "README.md", ".DS_Store")

        entries = fallback.scan(folder)

        assert [e.status for e in entries] == [fallback.MISSING] * 5

    def test_a_missing_workbook_is_reported_not_an_error(self, tmp_path) -> None:
        folder = _folder(tmp_path, "EKET.XLSX")
        sink = _Sink()

        entries = fallback.upload_folder(folder, sink)

        by_table = {e.table: e.status for e in entries}
        assert by_table.pop("eket") == fallback.UPLOADED
        assert set(by_table.values()) == {fallback.MISSING}
        assert list(sink.objects) == ["KPI 02 Data Extract/Tables/EKET.XLSX"]

    def test_an_absent_folder_is_five_missing_not_a_crash(self, tmp_path) -> None:
        entries = fallback.upload_folder(tmp_path / "nope", _Sink())

        assert [e.status for e in entries] == [fallback.MISSING] * 5

    def test_nothing_to_upload_never_opens_storage(self, tmp_path, monkeypatch) -> None:
        def no_storage():
            raise AssertionError("storage must not be opened with nothing to upload")

        monkeypatch.setattr(fallback, "get_storage", no_storage)

        fallback.upload_folder(tmp_path)

    def test_one_failed_upload_does_not_stop_the_rest(self, tmp_path) -> None:
        folder = _folder(tmp_path, "EKKO.XLSX", "EKET.XLSX")

        class Flaky(_Sink):
            @contextlib.contextmanager
            def open_write(self, key):
                if key.endswith("EKKO.XLSX"):
                    raise PermissionError("403 AuthorizationPermissionMismatch")
                with super().open_write(key) as buffer:
                    yield buffer

        entries = {e.table: e for e in fallback.upload_folder(folder, Flaky())}

        assert entries["ekko"].status == fallback.ERROR
        assert "403" in entries["ekko"].detail
        assert entries["eket"].status == fallback.UPLOADED

    def test_the_folder_comes_from_settings(self, tmp_path, monkeypatch) -> None:
        _folder(tmp_path, "EKKO.XLSX")
        monkeypatch.setattr(
            fallback, "get_settings",
            lambda: type("S", (), {"fallback_source_path": tmp_path})(),
        )
        sink = _Sink()

        fallback.upload_folder(storage=sink)

        assert "KPI 02 Data Extract/Tables/EKKO.XLSX" in sink.objects


class TestSync:
    def test_uploads_everything_then_loads_and_skips_absent_workbooks(self, tmp_path, monkeypatch) -> None:
        events: list[str] = []
        monkeypatch.setattr(fallback, "live_data_loaded", lambda sap_table: None)
        monkeypatch.setattr(
            fallback.seed_loader, "load_table",
            lambda spec, force=False: events.append(f"load:{spec.table}")
            or TableResult(spec.table, STATUS_SUCCEEDED, rows=7),
        )
        folder = _folder(tmp_path, "EKKO.XLSX", "30 Day GR Report.xlsx")

        summary, refused = fallback.sync(folder=folder, storage=_RecordingSink(events))

        assert events == [
            "upload:KPI 02 Data Extract/Tables/EKKO.XLSX",
            "upload:Resources Shared - Rohit/30 Day GR Report.xlsx",
            "load:ekko",
            "load:gr_30day",
        ]
        rows = {r.table: r for r in summary}
        assert (rows["ekko"].workbook_found, rows["ekko"].uploaded, rows["ekko"].load_status) == (
            True, "yes", STATUS_SUCCEEDED,
        )
        assert rows["ekko"].rows == 7
        assert (rows["eket"].workbook_found, rows["eket"].uploaded, rows["eket"].load_status) == (
            False, "skipped", None,
        )
        assert all(r.ok for r in summary)
        assert refused == []

    def test_named_tables_limit_both_upload_and_load(self, tmp_path, seed_calls) -> None:
        folder = _folder(tmp_path, "EKKO.XLSX", "EKET.XLSX")
        sink = _Sink()

        summary, refused = fallback.sync(["eket"], folder=folder, storage=sink)

        assert [r.table for r in summary] == ["eket"]
        assert list(sink.objects) == ["KPI 02 Data Extract/Tables/EKET.XLSX"]
        assert seed_calls == [("eket", True)]
        assert refused == []  # EKKO.XLSX is a fallback workbook, just not asked for

    def test_the_live_data_guard_still_refuses(self, tmp_path, seed_calls, monkeypatch) -> None:
        monkeypatch.setattr(
            fallback, "live_data_loaded",
            lambda sap_table: _Live() if sap_table == "EKKO" else None,
        )
        folder = _folder(tmp_path, "EKKO.XLSX", "EKET.XLSX")

        summary, _ = fallback.sync(folder=folder, storage=_Sink())

        rows = {r.table: r for r in summary}
        assert rows["ekko"].load_status == fallback.STATUS_REFUSED
        assert "FEKKO90000000" in rows["ekko"].reason
        assert not rows["ekko"].ok
        assert seed_calls == [("eket", True)]

    def test_force_is_passed_through(self, tmp_path, seed_calls, monkeypatch) -> None:
        monkeypatch.setattr(fallback, "live_data_loaded", lambda sap_table: _Live())

        summary, _ = fallback.sync(["ekko"], force=True, folder=_folder(tmp_path, "EKKO.XLSX"), storage=_Sink())

        assert summary[0].load_status == STATUS_SUCCEEDED
        assert seed_calls == [("ekko", True)]

    def test_try_sap_is_honoured(self, tmp_path, seed_calls, csv_route) -> None:
        route = csv_route(delivers=True)

        summary, _ = fallback.sync(
            ["ekko"], try_sap=True, folder=_folder(tmp_path, "EKKO.XLSX"), storage=_Sink(),
        )

        assert route.fired == ["EKKO"]
        assert seed_calls == []
        assert summary[0].rows == 3140

    def test_a_failed_upload_is_not_loaded(self, tmp_path, seed_calls) -> None:
        """Storage may still hold an older copy; loading it would pass it off
        as the file just synced."""

        class Broken(_Sink):
            @contextlib.contextmanager
            def open_write(self, key):
                raise PermissionError("403")
                yield  # pragma: no cover

        summary, _ = fallback.sync(["ekko"], folder=_folder(tmp_path, "EKKO.XLSX"), storage=Broken())

        assert seed_calls == []
        assert (summary[0].uploaded, summary[0].load_status) == (fallback.ERROR, None)
        assert not summary[0].ok

    def test_an_unknown_file_is_refused_and_named_during_sync(self, tmp_path, seed_calls) -> None:
        folder = _folder(tmp_path, "EKKO.XLSX", "MSEG_1.XLSX")
        sink = _Sink()

        summary, refused = fallback.sync(folder=folder, storage=sink)

        assert [f.name for f in refused] == ["MSEG_1.XLSX"]
        assert not any("MSEG" in key for key in sink.objects)
        assert seed_calls == [("ekko", True)]

    def test_an_unknown_table_name_is_refused_before_anything_happens(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="ekpo: not a fallback table"):
            fallback.sync(["ekpo"], folder=tmp_path, storage=_Sink())


class TestCli:
    """argparse wiring: the new forms, and the old ones unchanged."""

    @pytest.fixture
    def cli(self, tmp_path, monkeypatch, seed_calls):
        sink = _Sink()
        settings = type("S", (), {"fallback_source_path": tmp_path, "log_level": "INFO"})()
        monkeypatch.setattr(fallback, "get_settings", lambda: settings)
        monkeypatch.setattr(fallback, "configure_logging", lambda s: None)
        monkeypatch.setattr(fallback, "get_storage", lambda: sink)
        return tmp_path, sink

    def test_bare_upload_scans_the_folder_and_does_not_load(self, cli, seed_calls, capsys) -> None:
        folder, sink = cli
        _folder(folder, "EKKO.XLSX", "notes.txt")

        assert fallback.main(["--upload"]) == 0

        out = capsys.readouterr().out
        assert "KPI 02 Data Extract/Tables/EKKO.XLSX" in sink.objects
        assert "notes.txt" in out and "refused" in out
        assert "missing" in out
        assert seed_calls == []

    def test_upload_with_files_behaves_as_before(self, cli, capsys) -> None:
        folder, sink = cli
        _folder(folder, "EKKO.XLSX")

        assert fallback.main(["--upload", str(folder / "EKKO.XLSX")]) == 0
        assert fallback.main(["--upload", str(folder / "Mara.XLSX")]) == 2
        assert list(sink.objects) == ["KPI 02 Data Extract/Tables/EKKO.XLSX"]

    def test_sync_exits_zero_when_all_found_tables_load(self, cli, capsys) -> None:
        folder, _ = cli
        _folder(folder, "EKKO.XLSX")

        assert fallback.main(["--sync"]) == 0

        out = capsys.readouterr().out
        assert "WORKBOOK" in out and "UPLOADED" in out
        assert "ekko" in out and "skipped" in out

    def test_sync_exits_non_zero_when_a_table_is_refused(self, cli, monkeypatch, capsys) -> None:
        folder, _ = cli
        _folder(folder, "EKKO.XLSX")
        monkeypatch.setattr(fallback, "live_data_loaded", lambda sap_table: _Live())

        assert fallback.main(["--sync", "ekko"]) == 1
        assert "FEKKO90000000" in capsys.readouterr().out


# --- The live routes are untouched ------------------------------------------


def test_ekko_and_eket_are_no_longer_recorded_as_undelivered() -> None:
    """They deliver over CSV since 2026-09-30, so the live route fills them
    and the fallback's guard refuses a workbook over that data."""
    from app.ingest.csv_tables import csv_table

    assert csv_table("EKKO").blocked is None
    assert csv_table("EKET").blocked is None


class TestWorkbookWatermark:
    def test_ekko_seeds_the_delta_mark_from_its_newest_date(self, monkeypatch) -> None:
        written = []
        monkeypatch.setattr(fallback, "_newest_in_view", lambda view, col: "2026-09-25")
        monkeypatch.setattr(fallback, "set_watermark", lambda *a: written.append(a))

        assert fallback.seed_watermark("ekko") == "2026-09-24"
        assert written == [("PurchaseOrderSet", "Aedat", "2026-09-24", 0)]

    def test_reports_seed_nothing(self, monkeypatch) -> None:
        written = []
        monkeypatch.setattr(fallback, "set_watermark", lambda *a: written.append(a))

        assert fallback.seed_watermark("zmm065_gb") is None
        assert written == []

    def test_a_workbook_with_no_usable_date_seeds_nothing(self, monkeypatch) -> None:
        monkeypatch.setattr(fallback, "_newest_in_view", lambda view, col: None)
        assert fallback.seed_watermark("ekko") is None

    def test_the_seed_runs_after_a_successful_workbook_load(self, seed_calls, monkeypatch) -> None:
        seeded: list[str] = []
        monkeypatch.setattr(fallback, "seed_watermark", lambda t: seeded.append(t))

        fallback.load(["ekko", "gr_30day"])

        assert seeded == ["ekko", "gr_30day"]

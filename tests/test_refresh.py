"""The daily OData refresh: the setting's grammar, the window arithmetic, the
open-rows re-read, and the cycle's sequencing. Everything the cycle calls is
stubbed; no network, no database."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from app.ingest import refresh, scheduler
from app.ingest.manifest import spec_for
from app.ingest.refresh import OPEN_ROWS, RefreshEntry, Window, parse_entry, parse_sets, parse_window


# --- The grammar -------------------------------------------------------------


class TestParsing:
    def test_a_bare_name_reads_the_set_whole(self) -> None:
        entry = parse_entry("MaterialSet")
        assert entry == RefreshEntry(name="MaterialSet") and entry.whole and entry.error is None

    def test_a_name_with_a_window_reads_the_last_n_units(self) -> None:
        entry = parse_entry(" PurchaseRequisitionSet : 12m ")
        assert entry.name == "PurchaseRequisitionSet" and entry.window == Window(12, "m") and not entry.whole

    @pytest.mark.parametrize("text, window", [("90d", Window(90, "d")), ("2W", Window(2, "w")),
                                              ("12m", Window(12, "m")), ("1y", Window(1, "y"))])
    def test_every_unit(self, text, window) -> None:
        assert parse_window(text) == window

    @pytest.mark.parametrize("text", ["12", "12q", "m", "", "0m", "-3d", "twelve months"])
    def test_a_window_that_is_not_a_number_and_a_unit_is_refused(self, text) -> None:
        with pytest.raises(ValueError):
            parse_window(text)

    def test_an_unknown_set_carries_its_error_instead_of_raising(self) -> None:
        entry = parse_entry("NoSuchSet:12m")
        assert entry.error and "NoSuchSet" in entry.error

    def test_a_window_on_a_set_with_no_date_of_its_own_is_refused(self) -> None:
        # MBEW has no date SAP filters on: whole or nothing.
        entry = parse_entry("MaterialValuationSet:12m")
        assert entry.error and "no date of its own" in entry.error and "MaterialValuationSet" in entry.error

    def test_a_window_on_a_set_that_only_reads_through_parents_is_refused(self) -> None:
        # EKET has no window of its own; it rides on EKPO's and EKBE's.
        assert parse_entry("POScheduleLineSet:6m").error

    def test_a_bad_window_names_the_set(self) -> None:
        entry = parse_entry("MaterialSet:12q")
        assert entry.name == "MaterialSet" and entry.error and "12q" in entry.error

    def test_the_setting_is_a_comma_separated_list_and_blanks_are_ignored(self) -> None:
        entries = parse_sets("PurchaseRequisitionSet:12m, ,MaterialSet,")
        assert [(e.name, e.window) for e in entries] == [
            ("PurchaseRequisitionSet", Window(12, "m")), ("MaterialSet", None),
        ]
        assert parse_sets("") == [] and parse_sets(None) == []  # type: ignore[arg-type]

    def test_one_typo_costs_that_entry_not_the_list(self) -> None:
        entries = parse_sets("MaterialSet,Nope,ReservationItemSet:2y")
        assert [e.error is None for e in entries] == [True, False, True]

    def test_describe_reads_as_a_sentence(self) -> None:
        assert parse_entry("MaterialSet").describe() == "MaterialSet whole"
        assert parse_entry("ReservationItemSet:2y").describe() == "ReservationItemSet last 2y"
        assert parse_entry("Nope").describe().startswith("Nope (skipped: ")

    def test_the_default_setting_parses_clean(self) -> None:
        from app.core.config import Settings

        entries = parse_sets(Settings(_env_file=None).odata_refresh_sets)
        assert entries and all(e.error is None for e in entries)
        assert [e.name for e in entries] == [
            "PurchaseRequisitionSet", "ReservationItemSet", "MaterialSet", "MaterialPlantSet",
        ]


# --- The window ---------------------------------------------------------------


class TestWindowStart:
    def test_twelve_months_is_the_same_day_a_year_ago(self) -> None:
        assert Window(12, "m").start(date(2026, 11, 20)) == date(2025, 11, 20)

    def test_a_year_is_twelve_months(self) -> None:
        assert Window(1, "y").start(date(2026, 11, 20)) == Window(12, "m").start(date(2026, 11, 20))

    def test_months_clamp_to_the_shorter_month(self) -> None:
        assert Window(1, "m").start(date(2026, 3, 31)) == date(2026, 2, 28)
        assert Window(1, "y").start(date(2028, 2, 29)) == date(2027, 2, 28)

    def test_months_cross_the_year_boundary(self) -> None:
        assert Window(3, "m").start(date(2026, 1, 15)) == date(2025, 10, 15)
        assert Window(24, "m").start(date(2026, 1, 1)) == date(2024, 1, 1)

    def test_days_and_weeks_are_exact(self) -> None:
        assert Window(90, "d").start(date(2026, 11, 20)) == date(2026, 8, 22)
        assert Window(2, "w").start(date(2026, 11, 20)) == date(2026, 11, 6)

    def test_it_prints_as_written(self) -> None:
        assert str(Window(12, "m")) == "12m"


# --- Rows older than the window that are still open ---------------------------


class TestOpenRows:
    def test_only_the_sets_that_have_a_meaning_of_open(self) -> None:
        from app.ingest.raw_merge import sap_field

        assert set(OPEN_ROWS) == {"PurchaseRequisitionSet", "ReservationItemSet"}
        for name, rule in OPEN_ROWS.items():
            spec = spec_for(name)
            # Re-read by the set's own first key, which SAP filters on.
            assert rule.key == spec.keys[0]
            # Judged against the raw column behind the set's delta date.
            assert rule.date_column == sap_field(spec.delta.field)

    def test_the_query_selects_distinct_open_keys_dated_before_the_window_or_undated(self) -> None:
        spec = spec_for("PurchaseRequisitionSet")
        sql = refresh.open_rows_sql(spec, OPEN_ROWS[spec.name], "raw_eban")
        assert sql.startswith("SELECT DISTINCT [BANFN] FROM [raw_eban]")
        assert "ISNULL(LTRIM(EBELN), '') IN ('', '0000000000')" in sql
        assert "TRY_CONVERT(date, [BADAT], 104)" in sql and "IS NULL OR" in sql and "< :start" in sql

    def test_reservations_are_open_until_deleted_or_final_issued(self) -> None:
        spec = spec_for("ReservationItemSet")
        sql = refresh.open_rows_sql(spec, OPEN_ROWS[spec.name], "raw_resb")
        assert "SELECT DISTINCT [RSNUM] FROM [raw_resb]" in sql
        assert "XLOEK" in sql and "KZEAR" in sql and "[CRDAT]" in sql

    def test_numeric_keys_are_padded_to_the_declared_length(self) -> None:
        spec = spec_for("ReservationItemSet")
        assert refresh.pad_key("2061710", spec, "Rsnum") == "0002061710"
        assert refresh.pad_key(" 0002061710 ", spec, "Rsnum") == "0002061710"
        # Not numeric, or already full width: untouched.
        assert refresh.pad_key("ABC", spec, "Rsnum") == "ABC"
        assert refresh.pad_key("1000326544", spec_for("PurchaseRequisitionSet"), "Banfn") == "1000326544"

    def test_too_many_open_keys_is_refused_with_advice(self, monkeypatch) -> None:
        """More keys than the re-read is sized for is most of the table, not
        a tail: widen the window instead of re-reading it fifty keys at a time."""
        spec = spec_for("PurchaseRequisitionSet")

        class _Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k):
                class _R:
                    @staticmethod
                    def fetchall():
                        return [(str(1000000000 + i),) for i in range(refresh.OPEN_ROWS_KEY_LIMIT + 1)]
                return _R()

        class _Engine:
            def connect(self): return _Conn()

        with pytest.raises(RuntimeError, match="widen the window"):
            refresh.open_row_keys(spec, date(2025, 11, 20), engine=_Engine())

    def test_a_set_with_no_rule_has_no_open_rows(self) -> None:
        assert refresh.open_row_keys(spec_for("MaterialSet"), date(2025, 11, 20), engine=object()) == []

    def test_keys_come_back_padded_distinct_sorted_and_without_saps_none(self) -> None:
        """PRD's raw_resb carries '0000000000' -- no reservation -- on rows the
        CSV extract delivered; it is not a reservation to ask SAP for."""
        spec = spec_for("ReservationItemSet")

        class _Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, *a, **k):
                class _R:
                    @staticmethod
                    def fetchall():
                        return [("0000000000",), ("2061710",), ("0002061710",), ("0000004838",), (None,), ("",)]
                return _R()

        class _Engine:
            def connect(self): return _Conn()

        assert refresh.open_row_keys(spec, date(2025, 11, 20), engine=_Engine()) == ["0000004838", "0002061710"]


# --- The cycle ---------------------------------------------------------------


class _Fake:
    """fetch_set and load_set stubbed at their modules, which is where the
    cycle imports them from at call time."""

    def __init__(self, monkeypatch, *, fetch_ok=True, merge_ok=True, open_keys=()):
        from app.ingest import fetch, load, sap_system
        from app.ingest.fetch import FetchResult
        from app.ingest.load import STATUS_FAILED, STATUS_SUCCEEDED, LoadResult

        self.log: list[str] = []
        self.fetch_kwargs: list[dict] = []

        def fetch_set(spec, **kw):
            self.fetch_kwargs.append(kw)
            self.log.append(
                f"fetch {spec.name} {kw.get('mode')} since={kw.get('since')} advance={kw.get('advance_watermark')}"
            )
            r = FetchResult(entity_set=spec.name, prefix="p", mode=kw.get("mode"), rows=12, requests=2)
            if not fetch_ok:
                r.error = "boom"
            return r

        def load_set(spec, **kw):
            self.log.append(f"load {spec.name} prefix={kw.get('prefix')}")
            return LoadResult(spec.name, spec.raw_table, STATUS_SUCCEEDED if merge_ok else STATUS_FAILED,
                              rows=12, raw="raw_x: 3 changed of 10 matched, 2 inserted" if merge_ok else None,
                              error=None if merge_ok else "merge refused")

        monkeypatch.setattr(fetch, "fetch_set", fetch_set)
        monkeypatch.setattr(load, "load_set", load_set)
        monkeypatch.setattr(sap_system, "recorded", lambda: None)
        monkeypatch.setattr(refresh, "verified_deltas", lambda chosen: (list(chosen), {}))
        monkeypatch.setattr(refresh, "open_row_keys", lambda spec, start, **kw: list(open_keys))
        monkeypatch.setattr(
            refresh, "get_settings",
            lambda: type("S", (), {
                "ingest_prefix": "odata",
                "odata_refresh_sets": "PurchaseRequisitionSet:12m,MaterialSet",
                "odata_refresh_open_rows": True,
            })(),
        )


TODAY = date(2026, 11, 20)


class TestTheCycle:
    def test_a_whole_set_is_read_in_full_and_merged_without_touching_a_mark(self, monkeypatch) -> None:
        fake = _Fake(monkeypatch)

        summary = refresh.run_odata_refresh_cycle([parse_entry("MaterialSet")], today=TODAY)

        assert fake.log == ["fetch MaterialSet full since=None advance=False", "load MaterialSet prefix=p"]
        assert summary["pulled"] == 1 and summary["failed"] == 0 and summary["errors"] == []
        assert summary["sets"][0]["window"] == "whole" and summary["sets"][0]["rows"] == 12

    def test_a_windowed_set_is_read_from_the_window_start_as_a_delta_without_parents(self, monkeypatch) -> None:
        fake = _Fake(monkeypatch)

        summary = refresh.run_odata_refresh_cycle([parse_entry("PurchaseRequisitionSet:12m")], today=TODAY,
                                                  open_rows=False)

        assert fake.log == [
            "fetch PurchaseRequisitionSet delta since=2025-11-20 advance=False",
            "load PurchaseRequisitionSet prefix=p",
        ]
        kw = fake.fetch_kwargs[0]
        # No parent windows are read (parent_keys given, empty) and no mark moves.
        assert kw["parent_keys"] == [] and kw["advance_watermark"] is False and kw["extra_keys"] is None
        assert summary["sets"][0]["window"] == "last 12m (from 2025-11-20)"

    def test_the_landed_files_sit_apart_from_the_deltas(self, monkeypatch) -> None:
        fake = _Fake(monkeypatch)
        refresh.run_odata_refresh_cycle([parse_entry("MaterialSet")], today=TODAY)
        assert fake.fetch_kwargs[0]["root"] == "odata/refresh"

    def test_open_rows_older_than_the_window_are_re_read_by_the_sets_own_key(self, monkeypatch) -> None:
        fake = _Fake(monkeypatch, open_keys=["1000000001", "1000000002"])

        summary = refresh.run_odata_refresh_cycle([parse_entry("PurchaseRequisitionSet:12m")], today=TODAY)

        assert fake.fetch_kwargs[0]["extra_keys"] == ("Banfn", ["1000000001", "1000000002"])
        assert summary["sets"][0]["open_keys"] == 2

    def test_open_rows_can_be_switched_off_for_a_run(self, monkeypatch) -> None:
        fake = _Fake(monkeypatch, open_keys=["1000000001"])
        refresh.run_odata_refresh_cycle([parse_entry("PurchaseRequisitionSet:12m")], today=TODAY, open_rows=False)
        assert fake.fetch_kwargs[0]["extra_keys"] is None

    def test_a_whole_set_never_asks_for_open_rows(self, monkeypatch) -> None:
        fake = _Fake(monkeypatch, open_keys=["x"])
        refresh.run_odata_refresh_cycle([parse_entry("MaterialSet")], today=TODAY)
        assert "extra_keys" not in fake.fetch_kwargs[0]

    def test_the_configured_list_runs_in_order(self, monkeypatch) -> None:
        fake = _Fake(monkeypatch)

        summary = refresh.run_odata_refresh_cycle(today=TODAY, open_rows=False)

        assert [s["name"] for s in summary["sets"]] == ["PurchaseRequisitionSet", "MaterialSet"]
        assert fake.log[0].startswith("fetch PurchaseRequisitionSet delta") and fake.log[2].startswith("fetch MaterialSet full")

    def test_an_entry_the_setting_spelt_wrongly_is_counted_and_the_rest_run(self, monkeypatch) -> None:
        fake = _Fake(monkeypatch)

        summary = refresh.run_odata_refresh_cycle([parse_entry("Nope:12m"), parse_entry("MaterialSet")], today=TODAY)

        assert summary["pulled"] == 1 and summary["failed"] == 1
        assert summary["errors"] and summary["errors"][0].startswith("Nope:")
        assert fake.log == ["fetch MaterialSet full since=None advance=False", "load MaterialSet prefix=p"]

    def test_a_window_on_an_unmeasured_delta_date_is_refused_before_any_request(self, monkeypatch) -> None:
        """A filter SAP ignores answers HTTP 200 with the whole set, which
        would then be merged as 'the last 12 months'."""
        fake = _Fake(monkeypatch)
        monkeypatch.setattr(refresh, "verified_deltas", lambda chosen: ([], {chosen[0].name: "not probed"}))

        summary = refresh.run_odata_refresh_cycle([parse_entry("PurchaseRequisitionSet:12m")], today=TODAY)

        assert fake.log == [] and summary["failed"] == 1
        assert "not measured HONOURED" in summary["errors"][0] and "delta_probe" in summary["errors"][0]

    def test_a_failed_fetch_is_counted_not_raised_and_nothing_is_loaded(self, monkeypatch) -> None:
        fake = _Fake(monkeypatch, fetch_ok=False)
        summary = refresh.run_odata_refresh_cycle([parse_entry("MaterialSet")], today=TODAY)
        assert fake.log == ["fetch MaterialSet full since=None advance=False"]
        assert summary["failed"] == 1 and "boom" in summary["errors"][0]

    def test_a_refused_merge_is_counted_not_raised(self, monkeypatch) -> None:
        _Fake(monkeypatch, merge_ok=False)
        summary = refresh.run_odata_refresh_cycle([parse_entry("MaterialSet")], today=TODAY)
        assert summary["pulled"] == 0 and summary["failed"] == 1 and "merge refused" in summary["errors"][0]

    def test_the_wrong_sap_system_refuses_before_calling_sap(self, monkeypatch) -> None:
        from app.ingest import sap_system

        fake = _Fake(monkeypatch)
        monkeypatch.setattr(sap_system, "verdict", lambda recorded, configured: "database holds DEV, CPI_PATH is QA")

        summary = refresh.run_odata_refresh_cycle([parse_entry("MaterialSet")], today=TODAY)

        assert fake.log == [] and summary["failed"] == 1 and "refused" in summary["errors"][0]


# --- The timer -----------------------------------------------------------------


class TestTheDailyTick:
    """Like the weekly pull: its own mutex, then the delta's lock for the run,
    because both merge through the same staging tables."""

    @staticmethod
    def _locks(monkeypatch, *, delta_granted: bool = True):
        made: list[dict] = []

        class _Lock:
            def __init__(self, name: str = scheduler.LOCK_NAME, timeout_ms: int = 0) -> None:
                self.entry = {"name": name, "timeout_ms": timeout_ms, "released": False}
                made.append(self.entry)

            def acquire(self) -> bool:
                return delta_granted if self.entry["name"] == scheduler.LOCK_NAME else True

            def release(self) -> None:
                self.entry["released"] = True

        ran: list[bool] = []
        monkeypatch.setattr(scheduler, "_Lock", _Lock)
        monkeypatch.setattr(refresh, "run_odata_refresh_cycle",
                            lambda: ran.append(True) or {"pulled": 2, "failed": 0, "errors": [], "sets": []})
        return made, ran

    def test_it_waits_for_the_delta_lock_and_releases_both(self, monkeypatch) -> None:
        made, ran = self._locks(monkeypatch)

        asyncio.run(scheduler._odata_refresh_tick())

        assert ran == [True]
        assert [(m["name"], m["timeout_ms"]) for m in made] == [
            (scheduler.ODATA_REFRESH_LOCK_NAME, 0),
            (scheduler.LOCK_NAME, scheduler.FULL_PULL_DELTA_WAIT_MS),
        ]
        assert all(m["released"] for m in made)

    def test_a_delta_that_never_lets_go_skips_the_refresh_and_frees_its_own_lock(self, monkeypatch) -> None:
        made, ran = self._locks(monkeypatch, delta_granted=False)

        asyncio.run(scheduler._odata_refresh_tick())

        assert ran == []
        assert made[0]["name"] == scheduler.ODATA_REFRESH_LOCK_NAME and made[0]["released"]

    def test_the_lock_name_is_its_own(self) -> None:
        assert scheduler.ODATA_REFRESH_LOCK_NAME not in (
            scheduler.LOCK_NAME, scheduler.FULL_REFRESH_LOCK_NAME, scheduler.ODATA_FULL_PULL_LOCK_NAME,
        )

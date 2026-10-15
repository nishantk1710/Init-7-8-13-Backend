"""The CSV increment: a window pulled and merged by key, never replaced.

The merge runs against sqlite; the pull, the watermark table and the request
records are stubbed. What is tested is what the OData delta's tests test:
one row per key, update in place, insert the rest, re-run changes nothing,
and the mark moves only after a merge that succeeded.
"""

from __future__ import annotations

import contextlib
import io
from datetime import date

import pytest
from sqlalchemy import create_engine, text

from app.ingest import csv_delta, csv_load
from app.ingest.csv_tables import csv_table
from app.models.csv_extract import RECONCILE_INCREMENT, STATUS_COMPLETE, CsvExtractRequest

RESB = csv_table("RESB")
TODAY = date(2026, 10, 13)


@pytest.fixture(autouse=True)
def _resb_and_cdpos_without_an_odata_delta(monkeypatch):
    """The increment exists for the windowed tables the OData delta cannot
    cover, and it leaves out any table whose set has a runnable delta. RESB
    and CDPOS were those tables when it was written; since 2026-10-15 both
    have an OData delta (Crdat, Udate), so these tests pin the state the
    mechanism was built for rather than the live manifest."""
    from app.ingest import manifest as manifest_mod

    monkeypatch.delitem(manifest_mod.DELTAS, "ReservationItemSet", raising=False)
    monkeypatch.delitem(manifest_mod.DELTAS, "ChangeDocItemSet", raising=False)


class _Storage:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    @contextlib.contextmanager
    def open_read(self, key: str):
        buffer = io.BytesIO(self.objects[key])
        try:
            yield buffer
        finally:
            buffer.close()


@pytest.fixture
def engine(monkeypatch):
    engine = create_engine("sqlite://", future=True)
    from app.shared import sap_normalise

    monkeypatch.setattr(sap_normalise, "refresh_after_load", lambda table: None)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE raw_resb (MANDT TEXT, RSNUM TEXT, RSPOS TEXT, MATNR TEXT, BDMNG TEXT, LOGSY TEXT)"))
        c.execute(text("INSERT INTO raw_resb VALUES ('900','0000000001','0001','M1','5,000','kept')"))
        c.execute(text("INSERT INTO raw_resb VALUES ('900','0000000002','0001','M2','1,000','kept')"))
    return engine


def _count(engine, sql="SELECT COUNT(*) FROM raw_resb"):
    with engine.connect() as c:
        return c.execute(text(sql)).scalar()


def _csv(*rows: str) -> bytes:
    return ("MANDT,RSNUM,RSPOS,MATNR,BDMNG\r\n" + "".join(r + "\r\n" for r in rows)).encode()


# --- Keys ------------------------------------------------------------------------


def test_a_stripped_numc_key_is_padded_to_its_declared_width() -> None:
    assert csv_delta.pad_key("1", 4) == "0001"
    assert csv_delta.pad_key("0001", 4) == "0001"
    assert csv_delta.pad_key("12345", 4) == "12345", "never truncated"
    assert csv_delta.pad_key("A1", 4) == "A1", "not a number: kept"
    assert csv_delta.pad_key("1", None) == "1", "no declared width: kept"
    assert csv_delta.pad_key("", 4) == ""


def test_the_key_is_the_odata_identity_key_in_sap_spelling() -> None:
    keys, widths = csv_delta._key_columns(RESB)
    assert keys == ["RSNUM", "RSPOS"]
    assert all(isinstance(w, int) and w > 0 for w in widths.values())


# --- The merge ---------------------------------------------------------------------


class TestMerge:
    def test_rows_are_updated_in_place_and_new_ones_appended(self, engine) -> None:
        storage = _Storage({"k": _csv(
            '900,0000000001,0001,M1,"7,000"',     # existing: quantity changed
            '900,0000000003,0001,M3,"2,000"',     # new
            '900,,0001,M9,"9,000"',               # blank key: skipped
            '900,0000000003,0001,M3,"3,000"',     # same key again: last one wins
        )})
        out = csv_delta.merge_increment(RESB, "k", storage=storage, engine=engine)

        assert out.ok, out.error
        assert (out.rows, out.matched, out.updated, out.inserted, out.rows_without_key) == (4, 1, 1, 1, 1)
        assert _count(engine) == 3
        assert _count(engine, "SELECT BDMNG FROM raw_resb WHERE RSNUM='0000000001'") == "7,000"
        assert _count(engine, "SELECT BDMNG FROM raw_resb WHERE RSNUM='0000000003'") == "3,000"
        assert _count(engine, "SELECT LOGSY FROM raw_resb WHERE RSNUM='0000000001'") == "kept", \
            "a column the file does not carry survives the update"

    def test_running_the_same_window_twice_changes_nothing(self, engine) -> None:
        storage = _Storage({"k": _csv('900,0000000003,0001,M3,"2,000"')})
        first = csv_delta.merge_increment(RESB, "k", storage=storage, engine=engine)
        second = csv_delta.merge_increment(RESB, "k", storage=storage, engine=engine)

        assert (first.inserted, first.updated) == (1, 0)
        assert (second.matched, second.updated, second.inserted) == (1, 0, 0)
        assert not second.changed_anything
        assert _count(engine) == 3

    def test_a_key_the_csv_stripped_matches_the_padded_row(self, engine, monkeypatch) -> None:
        """CHANGENR arrived as 8 digits and BUZEI as 3 on 2026-10-13; the raw
        tables were padded that day. The increment pads before matching."""
        monkeypatch.setattr(csv_delta, "_key_columns", lambda spec: (["RSNUM", "RSPOS"], {"RSNUM": 10, "RSPOS": 4}))
        storage = _Storage({"k": _csv('900,1,1,M1,"9,000"')})
        out = csv_delta.merge_increment(RESB, "k", storage=storage, engine=engine)

        assert (out.matched, out.inserted, out.keys_padded) == (1, 0, 2)
        assert _count(engine) == 2
        assert _count(engine, "SELECT BDMNG FROM raw_resb WHERE RSNUM='0000000001' AND RSPOS='0001'") == "9,000"

    def test_a_new_column_in_the_file_is_added_to_the_table(self, engine) -> None:
        body = b'MANDT,RSNUM,RSPOS,MATNR,BDMNG,NEWCOL\r\n900,0000000004,0001,M4,"1,000",x\r\n'
        out = csv_delta.merge_increment(RESB, "k", storage=_Storage({"k": body}), engine=engine)

        assert out.ok and out.columns_added == ["NEWCOL"]
        assert _count(engine, "SELECT NEWCOL FROM raw_resb WHERE RSNUM='0000000004'") == "x"

    def test_a_missing_table_is_refused_not_created(self, engine) -> None:
        with engine.begin() as c:
            c.execute(text("DROP TABLE raw_resb"))
        out = csv_delta.merge_increment(RESB, "k", storage=_Storage({"k": _csv("900,1,1,M1,1")}), engine=engine)

        assert not out.ok and "does not exist" in out.error

    def test_a_file_without_the_key_columns_is_refused(self, engine) -> None:
        body = b"MANDT,MATNR,BDMNG\r\n900,M1,1\r\n"
        out = csv_delta.merge_increment(RESB, "k", storage=_Storage({"k": body}), engine=engine)

        assert not out.ok and "RSNUM" in out.error
        assert _count(engine) == 2

    def test_an_empty_file_leaves_the_table_alone(self, engine) -> None:
        out = csv_delta.merge_increment(RESB, "k", storage=_Storage({"k": _csv()}), engine=engine)

        assert out.ok and out.rows == 0 and not out.changed_anything
        assert _count(engine) == 2


# --- Which tables ---------------------------------------------------------------------


def test_tables_leave_out_anything_the_odata_delta_covers(monkeypatch) -> None:
    from app.ingest import manifest as manifest_mod

    monkeypatch.setattr(manifest_mod, "READ_BROKEN_SETS", {})
    chosen = [t.sap_table for t in csv_delta.tables(names=["RESB", "EKKO", "CDPOS", "NOPE"])]
    assert chosen == ["RESB", "CDPOS"], "EKKO has an OData delta; NOPE is not a table"


def test_the_default_list_is_the_two_windowed_tables_without_an_odata_delta() -> None:
    from app.core.config import Settings

    assert Settings(_env_file=None).csv_delta_table_list == ["RESB", "CDPOS"]
    assert Settings(_env_file=None).csv_delta_enabled is False


# --- One pass: the mark moves only after a merge ------------------------------------


class _Pull:
    def __init__(self, received=5, status=STATUS_COMPLETE, ok=True, fire_status="open"):
        self.received, self.status, self._ok, self.fire_status = received, status, ok, fire_status
        self.fired: list[tuple] = []

    def fire(self, sap_table, *, from_date, to_date, **kw):
        from app.ingest.csv_pull import PullResult

        self.fired.append((sap_table, from_date, to_date))
        return PullResult(sap_table, "FREQ1", self.fire_status, error=None if self.fire_status == "open" else "busy")

    def wait_for(self, request_id, **kw):
        from app.ingest.csv_pull import PullResult

        return PullResult("RESB", request_id, self.status, received_rows=self.received,
                          data_key="csv/RESB/FREQ1/RESB.csv" if self.received else None,
                          error=None if self._ok else "short")


@pytest.fixture
def harness(monkeypatch):
    marks: dict[str, str] = {"ReservationItemSet": "2026-10-06"}
    merged: list[str] = []
    loaded: list[str] = []

    pull = _Pull()
    monkeypatch.setattr(csv_delta.csv_pull, "fire", pull.fire)
    monkeypatch.setattr(csv_delta.csv_pull, "wait_for", pull.wait_for)
    monkeypatch.setattr(csv_delta, "get_watermark", lambda name, field: marks.get(name) if field == csv_delta.MARK_FIELD else None)
    monkeypatch.setattr(csv_delta, "set_watermark", lambda name, field, value, rows: marks.__setitem__(name, value))
    monkeypatch.setattr(csv_delta, "_tag_increment", lambda request_id: None)
    monkeypatch.setattr(csv_delta, "_request", lambda request_id: None)
    monkeypatch.setattr(csv_delta.csv_load, "_mark_loaded", lambda request_id: loaded.append(request_id))
    monkeypatch.setattr(csv_delta.csv_load, "_record", lambda *a, **k: None)
    monkeypatch.setattr(
        csv_delta, "merge_increment",
        lambda spec, key, **kw: (merged.append(key), csv_delta.MergeOutcome(spec.raw_table, rows=5, inserted=5))[1],
    )
    return pull, marks, merged, loaded


def test_a_merged_window_advances_the_mark_to_the_day_before_today(harness) -> None:
    pull, marks, merged, loaded = harness
    [result] = csv_delta.run_csv_delta(["RESB"], today=TODAY)

    assert pull.fired == [("RESB", "20261006", "20261013")]
    assert result.status == csv_delta.MERGED and result.mark == "2026-10-12"
    assert marks["ReservationItemSet"] == "2026-10-12"
    assert merged == ["csv/RESB/FREQ1/RESB.csv"] and loaded == ["FREQ1"]


def test_no_mark_means_skipped_never_a_whole_pull(harness) -> None:
    pull, marks, merged, loaded = harness
    del marks["ReservationItemSet"]
    [result] = csv_delta.run_csv_delta(["RESB"], today=TODAY)

    assert result.status == csv_delta.SKIPPED and "no CSV mark" in result.error
    assert pull.fired == [] and merged == []


def test_since_overrides_the_mark_for_this_run(harness) -> None:
    pull, marks, *_ = harness
    csv_delta.run_csv_delta(["RESB"], since="2026-09-01", today=TODAY)
    assert pull.fired == [("RESB", "20260901", "20261013")]
    assert marks["ReservationItemSet"] == "2026-10-12", "started before the mark: it joins on, so it moves"


def test_a_bounded_range_after_the_mark_moves_it_to_the_range_end(harness) -> None:
    pull, marks, *_ = harness
    [result] = csv_delta.run_csv_delta(["RESB"], until="20261009", today=TODAY)

    assert pull.fired == [("RESB", "20261006", "20261009")]
    assert result.status == csv_delta.MERGED and marks["ReservationItemSet"] == "2026-10-08"


def test_a_range_that_starts_after_the_mark_does_not_move_it(harness) -> None:
    """Pulling 10..12 Oct with the mark at 6 Oct leaves 6..9 Oct unread;
    advancing would make that gap permanent."""
    pull, marks, *_ = harness
    [result] = csv_delta.run_csv_delta(["RESB"], since="20261010", until="20261012", today=TODAY)

    assert pull.fired == [("RESB", "20261010", "20261012")]
    assert result.status == csv_delta.MERGED and result.mark is None
    assert marks["ReservationItemSet"] == "2026-10-06"


def test_a_range_ending_before_the_mark_never_moves_it_back(harness) -> None:
    pull, marks, *_ = harness
    csv_delta.run_csv_delta(["RESB"], since="20260901", until="20260930", today=TODAY)
    assert marks["ReservationItemSet"] == "2026-10-06"


def test_an_end_after_today_is_today_and_an_empty_range_is_skipped(harness) -> None:
    pull, marks, merged, _ = harness
    csv_delta.run_csv_delta(["RESB"], until="20261231", today=TODAY)
    assert pull.fired[-1] == ("RESB", "20261006", "20261013")
    [result] = csv_delta.run_csv_delta(["RESB"], since="20261012", until="20261010", today=TODAY)
    assert result.status == csv_delta.SKIPPED and "empty range" in result.error


def test_an_empty_window_holds_the_mark(harness) -> None:
    pull, marks, merged, loaded = harness
    pull.received, pull.status = 0, "timeout"
    [result] = csv_delta.run_csv_delta(["RESB"], today=TODAY)

    assert result.status == csv_delta.EMPTY
    assert marks["ReservationItemSet"] == "2026-10-06" and merged == []


def test_a_failed_merge_holds_the_mark(harness, monkeypatch) -> None:
    pull, marks, merged, loaded = harness
    monkeypatch.setattr(csv_delta, "merge_increment",
                        lambda spec, key, **kw: csv_delta.MergeOutcome(spec.raw_table, error="boom"))
    [result] = csv_delta.run_csv_delta(["RESB"], today=TODAY)

    assert result.status == csv_delta.FAILED and result.error == "boom"
    assert marks["ReservationItemSet"] == "2026-10-06" and loaded == []


def test_a_refused_fire_is_a_failure_with_the_reason(harness) -> None:
    pull, marks, merged, loaded = harness
    pull.fire_status = "failed"
    [result] = csv_delta.run_csv_delta(["RESB"], today=TODAY)

    assert result.status == csv_delta.FAILED and result.error == "busy"
    assert marks["ReservationItemSet"] == "2026-10-06"


# --- The whole-table loader never replaces a table from a window ---------------------


def test_csv_load_refuses_a_request_the_increment_fired() -> None:
    record = CsvExtractRequest(
        request_id="FREQ1", sap_table="RESB", entity_set="ReservationItemSet",
        from_date="20261006", to_date="20261013", status=STATUS_COMPLETE,
        reconcile=RECONCILE_INCREMENT, data_key="csv/RESB/FREQ1/RESB.csv",
    )
    result = csv_load.load_table("RESB", request=record, storage=_Storage({}))

    assert result.status == csv_load.STATUS_FAILED
    assert "windowed increment" in result.error


def test_the_scheduler_runs_the_csv_delta_only_when_switched_on(monkeypatch) -> None:
    from app.ingest import scheduler

    calls: list[str] = []
    monkeypatch.setattr(csv_delta, "run_csv_delta", lambda **kw: (calls.append("csv"), [])[1])
    monkeypatch.setattr("app.ingest.sap_system.recorded", lambda: None)
    monkeypatch.setattr("app.ingest.manifest.check_delta_filters", lambda: [])

    class _Report:
        baselined: list = []

        def summary(self):
            return {"fetched": 0, "loaded": 0, "rows": 0, "failed": 0, "advanced": {}, "errors": []}

    monkeypatch.setattr("app.ingest.sweep.run_delta_sweep", lambda *a, **k: _Report())
    monkeypatch.setattr("app.integrations.sap.client.SapClient", lambda: object())

    class _Settings:
        ingest_prefix = "odata"
        csv_delta_enabled = False

    monkeypatch.setattr(scheduler, "get_settings", lambda: _Settings())
    assert "csv_delta" not in scheduler.run_delta_cycle() and calls == []

    _Settings.csv_delta_enabled = True
    summary = scheduler.run_delta_cycle()
    assert calls == ["csv"] and summary["csv_delta"]["merged"] == 0

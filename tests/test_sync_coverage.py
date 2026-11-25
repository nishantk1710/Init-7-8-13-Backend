"""Coverage behind the watermark: the by-key fetch, the open-documents re-read,
the sync audit, the refresh retry and their timers. Everything SAP or the
database would answer is stubbed; no network, no database."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
from datetime import date, datetime, timezone

import pytest

from app.core.storage import ObjectNotFoundError, validate_key
from app.ingest import fetch, open_docs, refresh, scheduler, sync_audit, sync_rows
from app.ingest.manifest import spec_for

TODAY = date(2026, 11, 25)


class MemoryStorage:
    """The Storage port, backed by a dict (as tests/test_ingest.py)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    @contextlib.contextmanager
    def open_write(self, key: str):
        validate_key(key)
        buffer = io.BytesIO()
        yield buffer
        self.objects[key] = buffer.getvalue()

    @contextlib.contextmanager
    def open_read(self, key: str):
        validate_key(key)
        if key not in self.objects:
            raise ObjectNotFoundError(key)
        yield io.BytesIO(self.objects[key])


class FakeEngine:
    """Answers every statement with ``rows``; remembers what it was asked."""

    def __init__(self, rows=()) -> None:
        self.rows = list(rows)
        self.statements: list[tuple[str, dict]] = []

    @contextlib.contextmanager
    def connect(self):
        engine = self

        class _Result(list):
            def fetchall(self):
                return list(self)

        class _Conn:
            def execute(self, statement, params=None):
                engine.statements.append((str(statement), dict(params or {})))
                return _Result(engine.rows)

        yield _Conn()


# --- Settings: everything new is off by default ---------------------------------


class TestSettings:
    def _settings(self, **overrides):
        from app.core.config import Settings

        return Settings(_env_file=None, **overrides)

    def test_every_new_job_is_off_by_default(self) -> None:
        s = self._settings()
        assert s.open_docs_enabled is False
        assert s.sync_audit_enabled is False
        assert s.sync_audit_repair is False
        assert s.odata_refresh_retry_minutes == 0

    def test_the_default_hours_are_four_passes_a_day_with_the_refresh(self) -> None:
        s = self._settings()
        assert s.open_docs_hour_list == [5, 11, 17]
        assert s.odata_refresh_hour_utc not in s.open_docs_hour_list

    def test_hours_that_are_not_hours_are_left_out(self) -> None:
        s = self._settings(open_docs_hours_utc="25, x, 3,3, 0 ,23")
        assert s.open_docs_hour_list == [0, 3, 23]

    def test_every_default_set_has_a_rule(self) -> None:
        s = self._settings()
        assert set(s.open_docs_set_list) <= set(open_docs.OPEN_DOCS)
        assert set(s.sync_audit_set_list) <= set(sync_audit.AUDIT_RULES)


# --- fetch_set(keys_only=True) ------------------------------------------------------


class TestKeysOnlyFetch:
    def _run(self, monkeypatch, rows, keys):
        calls: list[tuple] = []

        def read_keys(client, spec, values, *, key_property=None):
            calls.append((spec.name, list(values), key_property))
            read = fetch._Read(expected=None)
            read.rows = list(rows)
            read.requests = 1
            return read

        monkeypatch.setattr(fetch, "_read_keys", read_keys)
        monkeypatch.setattr(fetch, "_read_direct", lambda *a, **k: pytest.fail("no window may be read"))
        monkeypatch.setattr(fetch, "collect_parent_keys", lambda *a, **k: pytest.fail("no parent may be read"))
        monkeypatch.setattr(fetch, "set_watermark", lambda *a, **k: pytest.fail("no mark may move"))
        monkeypatch.setattr(fetch, "get_watermark", lambda *a, **k: pytest.fail("no mark is looked at"))
        storage = MemoryStorage()
        result = fetch.fetch_set(
            spec_for("POScheduleLineSet"), root="odata/open_docs", client=object(), storage=storage,
            run_date=TODAY, keys_only=True, extra_keys=("Ebeln", keys),
        )
        return result, calls, storage

    def test_reads_only_the_keys_and_lands_an_increment(self, monkeypatch) -> None:
        rows = [{"Ebeln": "4100000001", "Ebelp": "00010", "Etenr": "0001"}]
        result, calls, storage = self._run(monkeypatch, rows, ["4100000001"])
        assert result.ok and result.rows == 1 and result.is_delta
        assert calls == [("POScheduleLineSet", ["4100000001"], "Ebeln")]
        manifest = json.loads(storage.objects[f"{result.prefix}/{fetch.MANIFEST_FILE}"])
        assert manifest["load_strategy"] == "merge" and manifest["usable"] is True
        assert result.prefix.startswith("odata/open_docs/")

    def test_no_keys_lands_an_empty_file_without_asking_sap(self, monkeypatch) -> None:
        result, calls, _ = self._run(monkeypatch, [], [])
        assert result.ok and result.rows == 0 and calls == []

    def test_a_row_twice_marks_the_file_unusable(self, monkeypatch) -> None:
        row = {"Ebeln": "4100000001", "Ebelp": "00010", "Etenr": "0001"}

        def read_keys(client, spec, values, *, key_property=None):
            read = fetch._Read(expected=None)
            read.rows = [row, dict(row)]
            read.duplicate_keys = 1
            read.requests = 2
            return read

        monkeypatch.setattr(fetch, "_read_keys", read_keys)
        result = fetch.fetch_set(
            spec_for("POScheduleLineSet"), root="odata/x", client=object(), storage=MemoryStorage(),
            run_date=TODAY, keys_only=True, extra_keys=("Ebeln", ["4100000001"]),
        )
        assert not result.ok and result.duplicate_keys == 1

    def test_a_failed_read_is_returned_not_raised(self, monkeypatch) -> None:
        def read_keys(*a, **k):
            raise RuntimeError("CPI 404")

        monkeypatch.setattr(fetch, "_read_keys", read_keys)
        result = fetch.fetch_set(
            spec_for("PurchaseOrderSet"), root="odata/x", client=object(), storage=MemoryStorage(),
            run_date=TODAY, keys_only=True, extra_keys=("Ebeln", ["4100000001"]),
        )
        assert not result.ok and "CPI 404" in result.error

    def test_without_keys_only_nothing_changes(self, monkeypatch) -> None:
        # The default path still reads the window: the new switch is opt-in.
        monkeypatch.setattr(fetch, "_fetch_keys_only", lambda *a, **k: pytest.fail("not opted in"))
        monkeypatch.setattr(fetch, "get_watermark", lambda *a, **k: None)
        monkeypatch.setattr(fetch, "_read_whole", lambda client, spec: fetch._Read(expected=0))
        result = fetch.fetch_set(
            spec_for("MaterialSet"), root="odata", client=object(), storage=MemoryStorage(), run_date=TODAY,
            mode=fetch.MODE_FULL, advance_watermark=False,
        )
        assert result.mode == fetch.MODE_FULL


# --- Which documents are open ----------------------------------------------------------


class TestOpenKeys:
    def test_every_rule_reads_back_by_a_property_the_set_has(self) -> None:
        for name, rule in open_docs.OPEN_DOCS.items():
            assert spec_for(name).entity_set.find(rule.key) is not None, name

    def test_a_purchase_order_is_one_document_across_its_three_sets(self) -> None:
        groups = {open_docs.OPEN_DOCS[n].group for n in ("PurchaseOrderSet", "PurchaseOrderItemSet", "POScheduleLineSet")}
        assert groups == {"purchase_orders"}

    def test_open_purchase_orders_are_read_through_the_views_and_bounded_by_due_date(self) -> None:
        sql = open_docs._open_purchase_orders_sql()
        assert "n_eket" in sql and "n_ekpo" in sql and ":start" in sql
        assert "delivery_completed" in sql and "deletion_indicator" in sql
        assert "scheduled_quantity" in sql and "qty_delivered" in sql

    def test_requisitions_and_reservations_reuse_the_refresh_rule_without_an_age_limit(self) -> None:
        assert refresh.OPEN_ROWS["PurchaseRequisitionSet"].predicate in open_docs._open_requisitions_sql()
        assert refresh.OPEN_ROWS["ReservationItemSet"].predicate in open_docs._open_reservations_sql()
        assert ":start" not in open_docs._open_reservations_sql()

    def test_keys_come_back_padded_distinct_sorted_without_saps_none(self) -> None:
        engine = FakeEngine([("4100000002",), ("4100000001",), ("4100000001",), ("0000000000",), (None,)])
        rule = open_docs.OPEN_DOCS["PurchaseOrderSet"]
        keys = open_docs.open_keys(spec_for("PurchaseOrderSet"), rule, start=TODAY, engine=engine)
        assert keys == ["4100000001", "4100000002"]
        assert engine.statements[0][1] == {"start": TODAY}

    def test_numeric_keys_are_padded_to_odata_length(self) -> None:
        engine = FakeEngine([("2061710",)])
        rule = open_docs.OPEN_DOCS["ReservationItemSet"]
        assert open_docs.open_keys(spec_for("ReservationItemSet"), rule, start=TODAY, engine=engine) == ["0002061710"]
        assert engine.statements[0][1] == {}

    def test_too_many_is_refused(self, monkeypatch) -> None:
        monkeypatch.setattr(refresh, "OPEN_ROWS_KEY_LIMIT", 2)
        engine = FakeEngine([("1",), ("2",), ("3",)])
        with pytest.raises(RuntimeError, match="above"):
            open_docs.open_keys(spec_for("PurchaseOrderSet"), open_docs.OPEN_DOCS["PurchaseOrderSet"],
                                start=TODAY, engine=engine)

    def test_months_back_clamps(self) -> None:
        assert open_docs._months_back(date(2026, 3, 31), 1) == date(2026, 2, 28)
        assert open_docs._months_back(date(2026, 11, 25), 12) == date(2025, 11, 25)


# --- The open-documents cycle --------------------------------------------------------------


class _Cycle:
    """fetch_set / load_set / the key query stubbed where the cycle imports them."""

    def __init__(self, monkeypatch, *, keys=("4100000001",), fetch_ok=True, merge_ok=True, missing=()):
        from app.ingest import load, sap_system
        from app.ingest.fetch import FetchResult
        from app.ingest.load import STATUS_FAILED, STATUS_SUCCEEDED, LoadResult

        self.key_queries: list[str] = []
        self.fetches: list[tuple] = []
        self.loads: list[str] = []
        self.checked: list[str] = []

        def open_keys(spec, rule, *, start, engine=None):
            self.key_queries.append(rule.group)
            return list(keys)

        def fetch_set(spec, **kw):
            self.fetches.append((spec.name, kw.get("keys_only"), kw.get("extra_keys"), kw.get("advance_watermark")))
            r = FetchResult(entity_set=spec.name, prefix=f"p/{spec.name}", mode=fetch.MODE_DELTA, rows=3, requests=1)
            if not fetch_ok:
                r.error = "boom"
            return r

        def load_set(spec, **kw):
            self.loads.append(spec.name)
            return LoadResult(spec.name, spec.raw_table, STATUS_SUCCEEDED if merge_ok else STATUS_FAILED,
                              raw="raw_x: 1 changed of 3 matched, 0 inserted" if merge_ok else None,
                              error=None if merge_ok else "merge refused")

        def check_missing(spec, rule, keys_, prefix, outcome):
            self.checked.append(spec.name)
            outcome.missing_in_sap = len(missing)
            outcome.missing_sample = list(missing)

        monkeypatch.setattr(open_docs, "open_keys", open_keys)
        monkeypatch.setattr(fetch, "fetch_set", fetch_set)
        monkeypatch.setattr(load, "load_set", load_set)
        monkeypatch.setattr(open_docs, "_check_missing", check_missing)
        monkeypatch.setattr(sap_system, "recorded", lambda: None)


PO_SETS = ["PurchaseOrderSet", "PurchaseOrderItemSet", "POScheduleLineSet"]


class TestOpenDocsCycle:
    def test_each_set_is_read_by_key_merged_and_checked_and_no_mark_moves(self, monkeypatch) -> None:
        fake = _Cycle(monkeypatch)
        summary = open_docs.run_open_docs_cycle(["POScheduleLineSet"], today=TODAY)
        assert summary["pulled"] == 1 and summary["failed"] == 0
        assert fake.fetches == [("POScheduleLineSet", True, ("Ebeln", ["4100000001"]), False)]
        assert fake.loads == ["POScheduleLineSet"] and fake.checked == ["POScheduleLineSet"]

    def test_a_purchase_orders_key_list_is_taken_once_for_its_three_sets(self, monkeypatch) -> None:
        fake = _Cycle(monkeypatch)
        open_docs.run_open_docs_cycle(PO_SETS, today=TODAY)
        assert fake.key_queries == ["purchase_orders"] and len(fake.fetches) == 3

    def test_nothing_open_merges_nothing(self, monkeypatch) -> None:
        fake = _Cycle(monkeypatch, keys=())
        summary = open_docs.run_open_docs_cycle(["PurchaseRequisitionSet"], today=TODAY)
        assert summary["pulled"] == 1 and fake.loads == [] and summary["sets"][0]["raw"] == "nothing open"

    def test_a_failed_fetch_is_counted_not_raised_and_nothing_is_loaded(self, monkeypatch) -> None:
        fake = _Cycle(monkeypatch, fetch_ok=False)
        summary = open_docs.run_open_docs_cycle(["ReservationItemSet"], today=TODAY)
        assert summary["failed"] == 1 and fake.loads == [] and "boom" in summary["errors"][0]

    def test_a_refused_merge_is_counted(self, monkeypatch) -> None:
        _Cycle(monkeypatch, merge_ok=False)
        summary = open_docs.run_open_docs_cycle(["ReservationItemSet"], today=TODAY)
        assert summary["failed"] == 1 and "merge refused" in summary["errors"][0]

    def test_a_set_with_no_rule_fails_alone(self, monkeypatch) -> None:
        _Cycle(monkeypatch)
        summary = open_docs.run_open_docs_cycle(["MaterialSet", "PurchaseRequisitionSet"], today=TODAY)
        assert summary["failed"] == 1 and summary["pulled"] == 1

    def test_rows_gone_in_sap_are_reported_and_summed(self, monkeypatch) -> None:
        _Cycle(monkeypatch, missing=("4100000001/10/2",))
        summary = open_docs.run_open_docs_cycle(["POScheduleLineSet"], today=TODAY)
        assert summary["missing_in_sap"] == 1 and summary["sets"][0]["missing_sample"] == ["4100000001/10/2"]

    def test_the_wrong_sap_system_refuses_before_calling_sap(self, monkeypatch) -> None:
        from app.ingest import sap_system

        fake = _Cycle(monkeypatch)
        monkeypatch.setattr(sap_system, "verdict", lambda recorded, configured: "PRD data, QA configured")
        summary = open_docs.run_open_docs_cycle(PO_SETS, today=TODAY)
        assert summary["failed"] == 3 and fake.fetches == []


# --- Matching our rows with SAP's --------------------------------------------------------------


def _shape(**kw):
    base = dict(table="raw_eket", vocabulary="sap",
                columns={"Ebeln": "EBELN", "Ebelp": "EBELP", "Etenr": "ETENR", "Eindt": "EINDT",
                         "Menge": "MENGE", "Wemng": "WEMNG"},
                numeric={"Menge": True, "Wemng": True}, edm={"Eindt": "Edm.DateTime"},
                identity_props=["Ebeln", "Ebelp", "Etenr"], identity_columns=["EBELN", "EBELP", "ETENR"])
    base.update(kw)
    return sync_rows.RawShape(**base)


class TestSyncRows:
    def test_keys_compare_without_their_padding(self) -> None:
        assert sync_rows.norm_key("0002061710") == sync_rows.norm_key("2061710") == "2061710"
        assert sync_rows.norm_key("0000") == "0" and sync_rows.norm_key(" A1 ") == "A1"
        assert sync_rows.norm_key(None) == ""

    def test_every_stored_spelling_of_a_key_is_looked_up(self) -> None:
        variants = sync_rows.key_variants(["2061710"], spec_for("ReservationItemSet"), "Rsnum")
        assert set(variants) == {"2061710", "0002061710"}

    def test_a_row_we_hold_that_sap_no_longer_returns_is_found(self) -> None:
        shape = _shape()
        sap = [{"Ebeln": "4100000001", "Ebelp": "00010", "Etenr": "0001"}]
        ours = [{"EBELN": "4100000001", "EBELP": "10", "ETENR": "1"},
                {"EBELN": "4100000001", "EBELP": "10", "ETENR": "2"}]
        assert sync_rows.missing_in_sap(sap, ours, shape) == [("4100000001", "10", "2")]


# --- The audit's comparison -----------------------------------------------------------------------


class TestCompare:
    @pytest.mark.parametrize("ours, theirs, numeric, fmt, verdict", [
        ("5", "5", False, "us", "same"),
        ("1.234,500", "1234.500", True, "german", "notation"),
        ("2,490.00", "2490.00", True, "us", "notation"),
        ("5,000-", "-5.000", True, "german", "notation"),
        ("10.000", "12.000", True, "us", "value"),
        (None, "0.000", True, "us", "notation"),
        ("20261124", "24.11.2026", False, "us", "notation"),
        ("2026-11-24", "24.11.2026", False, "us", "notation"),
        ("24.11.2026", "28.11.2026", False, "us", "value"),
        ("00000000", None, False, "us", "notation"),
        ("", "24.11.2026", False, "us", "empty_here"),
        ("00010", "10", False, "us", "notation"),
        (None, "", False, "us", "same"),
        ("", "X", False, "us", "empty_here"),
        ("X", "", False, "us", "value"),
        ("LAMP", "LAMP ", False, "us", "same"),
        ("LAMP", "BULB", False, "us", "value"),
    ])
    def test_values_compare_as_the_pages_read_them(self, ours, theirs, numeric, fmt, verdict) -> None:
        assert sync_audit.compare(ours, theirs, numeric=numeric, number_format=fmt) == verdict

    # Measured on PRD 25 Nov 2026: EBAN PREIS and BSMNG -- columns no view reads as a number --
    # kept the CSV extract's German notation, and the first audit called 29 of 54 PR rows changed.
    @pytest.mark.parametrize("ours, theirs, verdict", [
        ("158,83", "158.83", "notation"),
        ("12.640,68", "12640.68", "notation"),
        ("1,000", "     1.000", "notation"),       # BSMNG: German one; OData pads it
        ("0,000", "0.000", "notation"),
        ("5,000-", "-5.000", "notation"),
        ("2,490.00", "2490.00", "notation"),       # a US-notation extract reads too
        ("", "0.000", "notation"),                 # a blank quantity is a zero
        ("0,000", "", "notation"),
        ("158,83", "160.00", "value"),             # a price that really changed still shows
        ("", "5.000", "empty_here"),
        ("GRINDING; 50MM", "GRINDING, 50MM", "value"),
    ])
    def test_a_column_the_views_do_not_read_as_a_number_matches_in_any_notation(self, ours, theirs, verdict) -> None:
        assert sync_audit.compare(ours, theirs, numeric=False, number_format="us") == verdict

    @pytest.mark.parametrize("ours, theirs, verdict", [
        ("1,000", "1.000", "value"),    # the us view rule reads '1,000' as a thousand: the page is wrong
        ("", "5.000", "empty_here"),
        ("5.000", "", "value"),
    ])
    def test_a_column_the_views_read_as_a_number_keeps_the_view_rule(self, ours, theirs, verdict) -> None:
        assert sync_audit.compare(ours, theirs, numeric=True, number_format="us") == verdict


class TestAuditRows:
    def test_rows_are_sorted_into_same_notation_differ_and_missing(self) -> None:
        shape = _shape()
        sap = [
            {"Ebeln": "4100000001", "Ebelp": "00010", "Etenr": "0001", "Eindt": "2026-11-24T00:00:00",
             "Menge": "5.000", "Wemng": "0.000"},   # same date, notation-only quantity
            {"Ebeln": "4100000001", "Ebelp": "00010", "Etenr": "0002", "Eindt": "2026-12-01T00:00:00",
             "Menge": "8.000", "Wemng": "0.000"},   # delivery date moved in SAP
            {"Ebeln": "4100000001", "Ebelp": "00020", "Etenr": "0001", "Eindt": "2026-12-01T00:00:00",
             "Menge": "1.000", "Wemng": "0.000"},   # a line we do not have
        ]
        ours = [
            {"EBELN": "4100000001", "EBELP": "10", "ETENR": "1", "EINDT": "24.11.2026", "MENGE": "5,000", "WEMNG": "0,000"},
            {"EBELN": "4100000001", "EBELP": "10", "ETENR": "2", "EINDT": "24.11.2026", "MENGE": "8,000", "WEMNG": "0,000"},
            {"EBELN": "4100000001", "EBELP": "10", "ETENR": "3", "EINDT": "24.11.2026", "MENGE": "2,000", "WEMNG": "0,000"},
        ]
        outcome = sync_audit.AuditOutcome(name="POScheduleLineSet")
        sync_audit.audit_rows(spec_for("POScheduleLineSet"), shape, sap, ours, outcome, number_format="german")
        assert (outcome.same, outcome.notation, outcome.differ) == (0, 1, 1)
        assert outcome.missing_here == 1 and outcome.missing_in_sap == 1
        assert outcome.fields == {"Eindt": 1}
        assert outcome.examples and "Eindt" in outcome.examples[0]
        assert outcome.drift_rate == pytest.approx(0.5)

    def test_unconverted_numbers_are_notation_and_blanks_sap_fills_are_counted_apart(self) -> None:
        shape = _shape(table="raw_eban",
                       columns={"Banfn": "BANFN", "Bnfpo": "BNFPO", "Menge": "MENGE", "Preis": "PREIS",
                                "Bsmng": "BSMNG", "Bednr": "BEDNR", "Meins": "MEINS"},
                       numeric={"Menge": True}, edm={},
                       identity_props=["Banfn", "Bnfpo"], identity_columns=["BANFN", "BNFPO"])
        sap = [
            {"Banfn": "1000326001", "Bnfpo": "00010", "Menge": "60000.000", "Preis": "12640.68",
             "Bsmng": "     1.000", "Bednr": "", "Meins": "KG"},          # notation only
            {"Banfn": "1000326001", "Bnfpo": "00020", "Menge": "1.000", "Preis": "158.83",
             "Bsmng": "0.000", "Bednr": "SHUTDOWN", "Meins": "EA"},       # empty here, set in SAP
            {"Banfn": "1000326001", "Bnfpo": "00030", "Menge": "2.000", "Preis": "160.00",
             "Bsmng": "0.000", "Bednr": "SHUTDOWN", "Meins": "EA"},       # price changed too
        ]
        ours = [
            {"BANFN": "1000326001", "BNFPO": "00010", "MENGE": "60000.000", "PREIS": "12.640,68",
             "BSMNG": "1,000", "BEDNR": "", "MEINS": "KG"},
            {"BANFN": "1000326001", "BNFPO": "00020", "MENGE": "1.000", "PREIS": "158,83",
             "BSMNG": "0,000", "BEDNR": "", "MEINS": "EA"},
            {"BANFN": "1000326001", "BNFPO": "00030", "MENGE": "2.000", "PREIS": "158,83",
             "BSMNG": "0,000", "BEDNR": None, "MEINS": "EA"},
        ]
        outcome = sync_audit.AuditOutcome(name="PurchaseRequisitionSet")
        sync_audit.audit_rows(spec_for("PurchaseRequisitionSet"), shape, sap, ours, outcome, number_format="us")
        assert (outcome.same, outcome.notation, outcome.empty_here, outcome.differ) == (0, 1, 1, 1)
        assert outcome.fields == {"Preis": 1} and outcome.empty_fields == {"Bednr": 2}
        assert len(outcome.examples) == 1 and "Preis" in outcome.examples[0]
        assert outcome.drift_rate == pytest.approx(1 / 3)


class TestSampleKeys:
    def test_half_open_documents_and_the_rest_at_random_padded(self, monkeypatch) -> None:
        monkeypatch.setattr(open_docs, "open_keys", lambda spec, rule, *, start, engine=None: ["4100000009", "4100000008"])
        engine = FakeEngine([("4100000001",), ("4100000009",), ("4100000002",), ("0000000000",)])
        keys = sync_audit.sample_keys(spec_for("PurchaseOrderSet"), "Ebeln", "EBELN", "raw_ekko", 4,
                                      engine=engine, today=TODAY, rng=__import__("random").Random(1))
        assert len(keys) == 4 and len(set(keys)) == 4
        assert {"4100000009", "4100000008"} & set(keys)
        assert "0000000000" not in keys
        assert "NEWID()" in engine.statements[-1][0] and "TOP (" in engine.statements[-1][0]

    def test_a_set_with_no_open_rule_is_all_random(self, monkeypatch) -> None:
        monkeypatch.setattr(open_docs, "open_keys", lambda *a, **k: pytest.fail("no open rule for MARA"))
        engine = FakeEngine([("11251001",)])
        keys = sync_audit.sample_keys(spec_for("MaterialSet"), "Matnr", "MATNR", "raw_mara", 3, engine=engine)
        assert keys == ["000000000011251001"]


class TestAuditCycle:
    def _stub(self, monkeypatch, *, merge_ok=True):
        from app.core import db, storage
        from app.ingest import load, sap_system
        from app.ingest.fetch import FetchResult
        from app.ingest.load import STATUS_FAILED, STATUS_SUCCEEDED, LoadResult

        loads: list[str] = []
        monkeypatch.setattr(sap_system, "recorded", lambda: None)
        monkeypatch.setattr(db, "get_engine", lambda: object())
        monkeypatch.setattr(storage, "get_storage", lambda: object())
        monkeypatch.setattr(sync_rows, "raw_shape", lambda spec, engine: _shape())
        monkeypatch.setattr(sync_audit, "sample_keys", lambda *a, **k: ["4100000001"])
        monkeypatch.setattr(fetch, "fetch_set", lambda spec, **kw: FetchResult(
            entity_set=spec.name, prefix="p", mode=fetch.MODE_DELTA, rows=1))
        monkeypatch.setattr(sync_rows, "landed_rows", lambda storage_, prefix: [
            {"Ebeln": "4100000001", "Ebelp": "00010", "Etenr": "0001", "Menge": "5.000"}])
        monkeypatch.setattr(sync_rows, "db_rows_for_keys", lambda *a, **k: [
            {"EBELN": "4100000001", "EBELP": "10", "ETENR": "1", "MENGE": "4.000"}])
        monkeypatch.setattr(load, "load_set", lambda spec, **kw: loads.append(spec.name) or LoadResult(
            spec.name, spec.raw_table, STATUS_SUCCEEDED if merge_ok else STATUS_FAILED, raw="raw_eket: 1 changed",
            error=None if merge_ok else "refused"))
        return loads

    def test_read_only_by_default(self, monkeypatch) -> None:
        loads = self._stub(monkeypatch)
        summary = sync_audit.run_sync_audit_cycle(["POScheduleLineSet"], today=TODAY, repair=False)
        s = summary["sets"][0]
        assert summary["audited"] == 1 and loads == []
        assert s["differ"] == 1 and s["fields"] == {"Menge": 1} and s["drift_rate"] == 1.0
        assert s["empty_here"] == 0 and s["empty_fields"] == {}

    def test_the_cli_table_shows_what_is_empty_here(self, monkeypatch, capsys) -> None:
        from types import SimpleNamespace

        from app.ingest import __main__ as cli

        outcome = sync_audit.AuditOutcome(name="ReservationItemSet", documents=2, sap_rows=3, our_rows=3,
                                          same=1, empty_here=2, empty_fields={"Shkzg": 2, "Bednr": 1})
        monkeypatch.setattr(sync_audit, "run_sync_audit_cycle", lambda names, **kw: {
            "audited": 1, "failed": 0, "errors": [],
            "sets": [{**sync_audit.asdict(outcome), "drift_rate": outcome.drift_rate}]})
        assert cli._sync_audit(SimpleNamespace(set="ReservationItemSet", sample=None, repair=False)) == 0
        out = capsys.readouterr().out
        assert "EMPTY" in out and "empty here: Shkzgx2, Bednrx1" in out

    def test_repair_merges_the_sample(self, monkeypatch) -> None:
        loads = self._stub(monkeypatch)
        summary = sync_audit.run_sync_audit_cycle(["POScheduleLineSet"], today=TODAY, repair=True)
        assert loads == ["POScheduleLineSet"] and summary["sets"][0]["repaired"] == "raw_eket: 1 changed"

    def test_a_failed_repair_is_counted(self, monkeypatch) -> None:
        self._stub(monkeypatch, merge_ok=False)
        summary = sync_audit.run_sync_audit_cycle(["POScheduleLineSet"], today=TODAY, repair=True)
        assert summary["failed"] == 1 and "repair" in summary["errors"][0]

    def test_a_set_with_no_rule_fails_alone(self, monkeypatch) -> None:
        self._stub(monkeypatch)
        summary = sync_audit.run_sync_audit_cycle(["GatePassHeaderSet", "POScheduleLineSet"], today=TODAY, repair=False)
        assert summary["failed"] == 1 and summary["audited"] == 1


# --- Timers and the refresh retry -----------------------------------------------------------------


class TestSecondsUntilAny:
    def test_the_next_of_several_hours(self) -> None:
        now = datetime(2026, 11, 25, 6, 0, tzinfo=timezone.utc)
        assert scheduler.seconds_until_any([5, 11, 17], 20, now) == (5 * 60 + 20) * 60

    def test_past_the_last_hour_means_tomorrows_first(self) -> None:
        now = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)
        assert scheduler.seconds_until_any([5, 11, 17], 20, now) == (11 * 60 + 20) * 60

    def test_the_exact_moment_is_not_now(self) -> None:
        now = datetime(2026, 11, 25, 5, 20, tzinfo=timezone.utc)
        assert scheduler.seconds_until_any([5], 20, now) == 24 * 3600


def _locks(monkeypatch, *, delta_granted=True):
    made: list[dict] = []

    class _Lock:
        def __init__(self, name: str = scheduler.LOCK_NAME, timeout_ms: int = 0) -> None:
            self.entry = {"name": name, "timeout_ms": timeout_ms, "released": False}
            made.append(self.entry)

        def acquire(self) -> bool:
            return delta_granted if self.entry["name"] == scheduler.LOCK_NAME else True

        def release(self) -> None:
            self.entry["released"] = True

    monkeypatch.setattr(scheduler, "_Lock", _Lock)
    return made


class TestNewTicks:
    @pytest.mark.parametrize("tick, lock_name, module, func, summary", [
        ("_open_docs_tick", scheduler.OPEN_DOCS_LOCK_NAME, open_docs, "run_open_docs_cycle",
         {"pulled": 5, "failed": 0, "errors": [], "sets": [], "missing_in_sap": 0}),
        ("_sync_audit_tick", scheduler.SYNC_AUDIT_LOCK_NAME, sync_audit, "run_sync_audit_cycle",
         {"audited": 3, "failed": 0, "errors": [], "sets": []}),
    ])
    def test_own_lock_then_the_delta_lock_and_both_released(self, monkeypatch, tick, lock_name, module, func, summary) -> None:
        made = _locks(monkeypatch)
        ran: list[bool] = []
        monkeypatch.setattr(module, func, lambda: ran.append(True) or summary)
        asyncio.run(getattr(scheduler, tick)())
        assert ran == [True]
        assert [(m["name"], m["timeout_ms"]) for m in made] == [
            (lock_name, 0), (scheduler.LOCK_NAME, scheduler.FULL_PULL_DELTA_WAIT_MS),
        ]
        assert all(m["released"] for m in made)

    @pytest.mark.parametrize("tick, module, func", [
        ("_open_docs_tick", open_docs, "run_open_docs_cycle"),
        ("_sync_audit_tick", sync_audit, "run_sync_audit_cycle"),
    ])
    def test_a_delta_that_never_lets_go_skips_the_run(self, monkeypatch, tick, module, func) -> None:
        made = _locks(monkeypatch, delta_granted=False)
        monkeypatch.setattr(module, func, lambda: pytest.fail("must not run"))
        asyncio.run(getattr(scheduler, tick)())
        assert made[0]["released"]

    def test_the_lock_names_are_their_own(self) -> None:
        names = [scheduler.LOCK_NAME, scheduler.FULL_REFRESH_LOCK_NAME, scheduler.ODATA_FULL_PULL_LOCK_NAME,
                 scheduler.ODATA_REFRESH_LOCK_NAME, scheduler.OPEN_DOCS_LOCK_NAME, scheduler.SYNC_AUDIT_LOCK_NAME]
        assert len(set(names)) == len(names)


class TestRefreshRetry:
    def _settings(self, monkeypatch, minutes, sets="PurchaseRequisitionSet:12m,MaterialSet,NoSuchSet"):
        s = type("S", (), {"odata_refresh_retry_minutes": minutes, "odata_refresh_sets": sets})()
        monkeypatch.setattr(scheduler, "get_settings", lambda: s)

    def _sleeps(self, monkeypatch) -> list[float]:
        slept: list[float] = []

        async def sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(scheduler.asyncio, "sleep", sleep)
        return slept

    FAILED = {"pulled": 1, "failed": 2, "errors": [], "sets": [
        {"name": "PurchaseRequisitionSet", "error": None},
        {"name": "MaterialSet", "error": "NotFoundError (HTTP 404)"},
        {"name": "NoSuchSet", "error": "unknown set"},
    ]}

    def test_off_by_default_never_retries(self, monkeypatch) -> None:
        self._settings(monkeypatch, 0)
        slept = self._sleeps(monkeypatch)
        monkeypatch.setattr(refresh, "run_odata_refresh_cycle", lambda *a, **k: pytest.fail("no retry"))
        asyncio.run(scheduler._odata_refresh_retry(self.FAILED))
        assert slept == []

    def test_nothing_failed_nothing_retried(self, monkeypatch) -> None:
        self._settings(monkeypatch, 15)
        slept = self._sleeps(monkeypatch)
        asyncio.run(scheduler._odata_refresh_retry({"pulled": 3, "failed": 0, "errors": [], "sets": []}))
        assert slept == []

    def test_only_the_failed_valid_sets_are_retried_once_after_the_wait(self, monkeypatch) -> None:
        self._settings(monkeypatch, 15)
        slept = self._sleeps(monkeypatch)
        made = _locks(monkeypatch)
        retried: list[list[str]] = []
        monkeypatch.setattr(refresh, "run_odata_refresh_cycle", lambda entries: retried.append(
            [e.name for e in entries]) or {"pulled": 1, "failed": 0, "errors": [], "sets": []})
        asyncio.run(scheduler._odata_refresh_retry(self.FAILED))
        assert slept == [15 * 60]
        assert retried == [["MaterialSet"]]
        assert [m["name"] for m in made] == [scheduler.ODATA_REFRESH_LOCK_NAME, scheduler.LOCK_NAME]
        assert all(m["released"] for m in made)

    def test_the_nightly_tick_with_nothing_failed_is_exactly_as_before(self, monkeypatch) -> None:
        made = _locks(monkeypatch)
        slept = self._sleeps(monkeypatch)
        monkeypatch.setattr(refresh, "run_odata_refresh_cycle",
                            lambda: {"pulled": 5, "failed": 0, "errors": [], "sets": []})
        asyncio.run(scheduler._odata_refresh_tick())
        assert [m["name"] for m in made] == [scheduler.ODATA_REFRESH_LOCK_NAME, scheduler.LOCK_NAME]
        assert slept == []


class TestStartRegistersTheNewTimers:
    def _settings(self, monkeypatch, **overrides):
        from app.core.config import Settings

        base = dict(database_url="mssql+pyodbc://x", storage_url="abfss://y", cpi_base_url="", cpi_token_url="",
                    cpi_client_id="", cpi_client_secret="")
        base.update(overrides)
        s = Settings(_env_file=None, **base)
        monkeypatch.setattr(scheduler, "get_settings", lambda: s)
        return s

    def test_nothing_new_starts_when_nothing_is_on(self, monkeypatch) -> None:
        self._settings(monkeypatch)
        assert scheduler.start(None) is None

    @pytest.mark.parametrize("flag", ["open_docs_enabled", "sync_audit_enabled"])
    def test_each_alone_starts_a_task(self, monkeypatch, flag) -> None:
        self._settings(monkeypatch, **{flag: True})

        async def run():
            task = scheduler.start(None)
            assert task is not None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        asyncio.run(run())

    def test_open_docs_with_no_valid_hour_does_not_start(self, monkeypatch) -> None:
        self._settings(monkeypatch, open_docs_enabled=True, open_docs_hours_utc="x,99")
        assert scheduler.start(None) is None

"""OData rows into the raw table: matching, updating in place, no duplicates.

Run against SQLite. The merge is written in portable SQL for exactly this
reason: the statements that decide correctness -- the correlated UPDATE, the
NOT EXISTS insert, the key match -- behave the same here as on SQL Server,
and a database is the only honest way to test them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from app.ingest import raw_merge
from app.ingest.manifest import spec_for

EKPO = spec_for("PurchaseOrderItemSet")
EKKO = spec_for("PurchaseOrderSet")
MARC = spec_for("MaterialPlantSet")


@pytest.fixture
def engine(monkeypatch):
    engine = create_engine("sqlite://", future=True)
    # Column additions rebuild the normalise view; that is a database call
    # against the real layer, so here it is only recorded.
    from app.shared import sap_normalise
    rebuilt: list[str] = []
    monkeypatch.setattr(sap_normalise, "refresh_after_load", lambda t: rebuilt.append(t) or True)
    engine.rebuilt = rebuilt  # type: ignore[attr-defined]
    return engine


def csv_shaped_ekpo(engine, rows):
    """raw_ekpo as the CSV route creates it: SAP field names, DD.MM.YYYY, and
    columns the OData set never carries (ZZKTWRT stands in for the other 257)."""
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE raw_ekpo (EBELN TEXT, EBELP TEXT, LOEKZ TEXT, AEDAT TEXT, TXZ01 TEXT, "
            "MATNR TEXT, WERKS TEXT, LGORT TEXT, BEDNR TEXT, MENGE TEXT, MEINS TEXT, NETPR TEXT, "
            "PEINH TEXT, NETWR TEXT, ELIKZ TEXT, PSTYP TEXT, KNTTP TEXT, PLIFZ TEXT, BANFN TEXT, "
            "BNFPO TEXT, ZZKTWRT TEXT)"))
        for r in rows:
            c.execute(text(
                "INSERT INTO raw_ekpo (EBELN, EBELP, AEDAT, MATNR, MENGE, NETPR, ZZKTWRT) "
                "VALUES (:e, :p, :a, :m, :q, :n, :z)"), r)


def fetch(engine, sql):
    with engine.connect() as c:
        return c.execute(text(sql)).fetchall()


def count(engine, table):
    return fetch(engine, f"SELECT COUNT(*) FROM {table}")[0][0]


# --- Names and values -------------------------------------------------------


class TestNames:
    def test_odata_property_to_sap_field(self) -> None:
        assert raw_merge.sap_field("Ebeln") == "EBELN"
        assert raw_merge.sap_field("Txz01") == "TXZ01"
        assert raw_merge.sap_field("BudatMkpf") == "BUDAT_MKPF"
        assert raw_merge.sap_field("XblnrMkpf") == "XBLNR_MKPF"

    def test_every_csv_route_set_has_a_raw_table(self) -> None:
        from app.ingest.manifest import specs
        assert all(raw_merge.raw_table_for(s) is not None for s in specs())


class TestShape:
    def test_a_sap_date_lands_on_its_calendar_day_not_the_utc_one(self) -> None:
        """SAP serialises 15-Sep midnight local; the envelope decodes 14-Sep 22:00 UTC."""
        decoded = datetime(2026, 9, 14, 22, 0, tzinfo=timezone.utc)
        assert raw_merge.shape(decoded, "Edm.DateTime", raw_merge.VOCAB_SAP) == "15.09.2026"
        assert raw_merge.shape(decoded, "Edm.DateTime", raw_merge.VOCAB_WORKBOOK) == "2026-09-15"

    def test_a_landed_iso_string_is_shaped_the_same_way(self) -> None:
        assert raw_merge.shape("2026-09-14T22:00:00+00:00", "Edm.DateTime", raw_merge.VOCAB_SAP) == "15.09.2026"

    def test_booleans_become_sap_flags(self) -> None:
        assert raw_merge.shape(True, "Edm.Boolean", raw_merge.VOCAB_SAP) == "X"
        assert raw_merge.shape(False, "Edm.Boolean", raw_merge.VOCAB_SAP) == ""
        assert raw_merge.shape("true", "Edm.Boolean", raw_merge.VOCAB_SAP) == "X"

    def test_decimals_are_text_without_exponent(self) -> None:
        assert raw_merge.shape(Decimal("12.500"), "Edm.Decimal", raw_merge.VOCAB_SAP) == "12.500"
        assert raw_merge.shape(Decimal("2E+3"), "Edm.Decimal", raw_merge.VOCAB_SAP) == "2000"

    def test_none_stays_none(self) -> None:
        assert raw_merge.shape(None, "Edm.String", raw_merge.VOCAB_SAP) is None


# --- Resolution -------------------------------------------------------------


class TestResolve:
    def test_csv_shaped_table_resolves_to_sap_fields(self) -> None:
        vocab, resolved, missing = raw_merge.resolve(EKPO, "ekpo", ["EBELN", "EBELP", "AEDAT", "MATNR"])
        assert vocab == raw_merge.VOCAB_SAP and missing == []
        by_prop = {r.prop: r for r in resolved}
        assert by_prop["Ebeln"].column == "EBELN" and by_prop["Ebeln"].is_key
        assert by_prop["Aedat"].column == "AEDAT" and not by_prop["Aedat"].added
        assert by_prop["Menge"].column == "MENGE" and by_prop["Menge"].added

    def test_workbook_shaped_table_resolves_to_labels(self) -> None:
        present = ["purchasing_document", "created_on", "currency", "supplier"]
        vocab, resolved, missing = raw_merge.resolve(EKKO, "ekko", present)
        assert vocab == raw_merge.VOCAB_WORKBOOK and missing == []
        by_prop = {r.prop: r for r in resolved}
        assert by_prop["Ebeln"].column == "purchasing_document"
        assert by_prop["Aedat"].column == "created_on"
        assert by_prop["Lifnr"].column == "supplier"
        assert by_prop["Ekorg"].column == "EKORG" and by_prop["Ekorg"].added

    def test_matching_is_case_insensitive_and_keeps_the_table_spelling(self) -> None:
        _, resolved, _ = raw_merge.resolve(EKPO, "ekpo", ["ebeln", "Ebelp"])
        assert {r.prop: r.column for r in resolved if r.is_key} == {"Ebeln": "ebeln", "Ebelp": "Ebelp"}

    def test_a_missing_key_column_is_reported_not_added(self) -> None:
        vocab, resolved, missing = raw_merge.resolve(EKPO, "ekpo", ["EBELN", "AEDAT"])
        assert missing == ["Ebelp"] and resolved == []


# --- The merge --------------------------------------------------------------


class TestMerge:
    def test_existing_rows_are_updated_in_place_and_wide_columns_survive(self, engine) -> None:
        csv_shaped_ekpo(engine, [
            {"e": "4100001503", "p": "00010", "a": "01.01.2019", "m": "000000000010000001",
             "q": "5.000", "n": "100.00", "z": "kept"},
        ])
        result = raw_merge.merge(EKPO, [{
            "Ebeln": "4100001503", "Ebelp": "00010", "Aedat": datetime(2026, 9, 14, 22, 0, tzinfo=timezone.utc),
            "Matnr": "000000000010000001", "Menge": Decimal("7.000"), "Netpr": Decimal("120.00"),
        }], engine=engine)

        assert result.status == raw_merge.MERGED
        assert (result.updated, result.inserted) == (1, 0)
        row = fetch(engine, "SELECT AEDAT, MENGE, NETPR, ZZKTWRT FROM raw_ekpo")[0]
        assert tuple(row) == ("15.09.2026", "7.000", "120.00", "kept")
        assert count(engine, "raw_ekpo") == 1

    def test_new_rows_are_inserted(self, engine) -> None:
        csv_shaped_ekpo(engine, [{"e": "4100001503", "p": "00010", "a": "01.01.2019",
                                  "m": "M", "q": "1", "n": "1", "z": "kept"}])
        result = raw_merge.merge(EKPO, [
            {"Ebeln": "4100001503", "Ebelp": "00020", "Menge": Decimal("2")},
            {"Ebeln": "4100009999", "Ebelp": "00010", "Menge": Decimal("3")},
        ], engine=engine)

        assert (result.updated, result.inserted) == (0, 2)
        assert count(engine, "raw_ekpo") == 3
        assert fetch(engine, "SELECT ZZKTWRT FROM raw_ekpo WHERE EBELP='00020'")[0][0] is None

    def test_running_the_same_delta_twice_changes_nothing(self, engine) -> None:
        """The property every incremental load needs: idempotent on the key."""
        csv_shaped_ekpo(engine, [])
        rows = [{"Ebeln": "1", "Ebelp": "00010", "Menge": Decimal("2")},
                {"Ebeln": "1", "Ebelp": "00020", "Menge": Decimal("3")}]

        first = raw_merge.merge(EKPO, rows, engine=engine)
        second = raw_merge.merge(EKPO, rows, engine=engine)

        assert (first.updated, first.inserted) == (0, 2)
        assert (second.updated, second.inserted) == (2, 0)
        assert count(engine, "raw_ekpo") == 2

    def test_a_row_repeated_within_one_batch_is_inserted_once(self, engine) -> None:
        """A chunked read can return the same row from two chunks."""
        csv_shaped_ekpo(engine, [])
        result = raw_merge.merge(EKPO, [
            {"Ebeln": "1", "Ebelp": "00010", "Menge": Decimal("2")},
            {"Ebeln": "1", "Ebelp": "00010", "Menge": Decimal("9")},   # later wins
        ], engine=engine)

        assert result.inserted == 1
        assert fetch(engine, "SELECT MENGE FROM raw_ekpo")[0][0] == "9"

    def test_rows_without_a_complete_key_are_skipped_and_counted(self, engine) -> None:
        csv_shaped_ekpo(engine, [])
        result = raw_merge.merge(EKPO, [
            {"Ebeln": "1", "Ebelp": None, "Menge": Decimal("2")},
            {"Ebeln": "", "Ebelp": "00010"},
            {"Ebeln": "2", "Ebelp": "00010"},
        ], engine=engine)

        assert result.inserted == 1 and result.rows_without_key == 2

    def test_a_property_the_table_lacks_gets_a_column_and_the_view_is_rebuilt(self, engine) -> None:
        """raw_marc over CSV has no DISMM/PLIFZ; the OData MaterialPlantSet does."""
        with engine.begin() as c:
            c.execute(text("CREATE TABLE raw_marc (MATNR TEXT, WERKS TEXT, ZZCRITIC TEXT)"))
            c.execute(text("INSERT INTO raw_marc VALUES ('000000000010000001', '1300', 'A')"))

        result = raw_merge.merge(MARC, [{
            "Matnr": "000000000010000001", "Werks": "1300", "Dismm": "PD", "Plifz": "14",
            "Lvorm": False, "Minbe": Decimal("2.000"),
        }], engine=engine)

        assert result.status == raw_merge.MERGED and result.updated == 1
        assert "DISMM" in result.columns_added and "PLIFZ" in result.columns_added
        row = fetch(engine, "SELECT DISMM, PLIFZ, LVORM, MINBE, ZZCRITIC FROM raw_marc")[0]
        assert tuple(row) == ("PD", "14", "", "2.000", "A")
        assert engine.rebuilt == ["marc"]

    def test_a_workbook_shaped_table_is_updated_in_its_own_vocabulary(self, engine) -> None:
        """EKKO comes from the fallback workbook: labels and ISO dates."""
        with engine.begin() as c:
            # The labels the July EKKO workbook carries for the OData set's fields;
            # EKORG has no label there, so it is the one column the merge must add.
            c.execute(text("CREATE TABLE raw_ekko (purchasing_document TEXT, purchasing_doc_type TEXT, "
                           "created_on TEXT, document_date TEXT, supplier TEXT, purchasing_group TEXT, "
                           "currency TEXT, extra_label TEXT)"))
            c.execute(text("INSERT INTO raw_ekko VALUES ('4100001503', 'NB', '2019-01-01', '2019-01-01', "
                           "'V1', '001', 'ZAR', 'kept')"))

        result = raw_merge.merge(EKKO, [{
            "Ebeln": "4100001503", "Aedat": datetime(2026, 9, 14, 22, 0, tzinfo=timezone.utc),
            "Waers": "USD", "Lifnr": "V2", "Ekorg": "1000",
        }], engine=engine)

        assert result.vocabulary == raw_merge.VOCAB_WORKBOOK
        assert result.updated == 1 and result.columns_added == ["EKORG"]
        row = fetch(engine, "SELECT created_on, currency, supplier, EKORG, extra_label FROM raw_ekko")[0]
        assert tuple(row) == ("2026-09-15", "USD", "V2", "1000", "kept")

    def test_no_table_yet_is_skipped_with_the_command_to_build_it(self, engine) -> None:
        result = raw_merge.merge(EKPO, [{"Ebeln": "1", "Ebelp": "00010"}], engine=engine)

        assert result.status == raw_merge.SKIPPED and result.ok
        assert "--csv-pull" in result.detail and "EKPO" in result.detail

    def test_a_table_without_the_key_columns_is_refused(self, engine) -> None:
        with engine.begin() as c:
            c.execute(text("CREATE TABLE raw_ekpo (EBELN TEXT, MENGE TEXT)"))

        result = raw_merge.merge(EKPO, [{"Ebeln": "1", "Ebelp": "00010"}], engine=engine)

        assert result.status == raw_merge.REFUSED and not result.ok
        assert "Ebelp" in result.detail
        assert count(engine, "raw_ekpo") == 0

    def test_no_staging_table_is_left_behind(self, engine) -> None:
        csv_shaped_ekpo(engine, [])
        raw_merge.merge(EKPO, [{"Ebeln": "1", "Ebelp": "00010"}], engine=engine)
        from sqlalchemy import inspect
        assert not inspect(engine).has_table("raw_ekpo__odata_stg")


# --- From a landed fetch -----------------------------------------------------


class _Storage:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def open_read(self, key):
        import contextlib, io
        @contextlib.contextmanager
        def cm():
            yield io.BytesIO(self.objects[key])
        return cm()


class TestMergeLanded:
    def test_rows_from_the_jsonl_are_merged(self, engine) -> None:
        csv_shaped_ekpo(engine, [])
        manifest = {"usable": True, "data_file": "data.jsonl"}
        rows = [{"Ebeln": "1", "Ebelp": "00010", "Aedat": "2026-09-14T22:00:00+00:00", "Menge": "2.000"}]
        storage = _Storage({
            "p/_manifest.json": json.dumps(manifest).encode(),
            "p/data.jsonl": "\n".join(json.dumps(r) for r in rows).encode(),
        })

        result = raw_merge.merge_landed(EKPO, "p", storage=storage, engine=engine)

        assert result.inserted == 1
        assert fetch(engine, "SELECT AEDAT FROM raw_ekpo")[0][0] == "15.09.2026"

    def test_an_unusable_fetch_is_refused_before_any_row_is_read(self, engine) -> None:
        csv_shaped_ekpo(engine, [])
        storage = _Storage({"p/_manifest.json": json.dumps({"usable": False, "duplicate_keys": 3}).encode()})

        result = raw_merge.merge_landed(EKPO, "p", storage=storage, engine=engine)

        assert result.status == raw_merge.REFUSED and "unusable" in result.detail
        assert count(engine, "raw_ekpo") == 0

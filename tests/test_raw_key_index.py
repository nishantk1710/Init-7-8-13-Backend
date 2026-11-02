"""The key index the CSV full load builds on raw_<table>, and the merges that use it.

Every merge into a raw table matches on the set's identity key; unindexed, each
one read the whole table (7.2 million rows for MSEG). The load now gives the
key columns a bounded width and indexes them, and falls back to the table as
it was built before -- every column nvarchar(max), no index -- if the index is
what failed.

The load runs against a fake cursor that enforces column widths the way SQL
Server does; the merges run on SQLite. The SQL Server facts -- the widths fit
the 1700-byte key limit, and an overlong value fails with the SQLSTATE the
fallback looks for -- are tested against the real thing where one is
configured (CI's SQL Server container).
"""

from __future__ import annotations

import contextlib
import io
import re
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import NVARCHAR, Unicode, UnicodeText, create_engine, inspect, text
from sqlalchemy.exc import DataError

from app.core.config import get_settings
from app.core.db import MSSQL, backend_of
from app.ingest import csv_delta, csv_load, raw_merge
from app.ingest.csv_tables import CSV_TABLES, csv_table
from app.ingest.manifest import spec_for
from app.ingest.raw_merge import sap_field, staging_type
from app.models.csv_extract import STATUS_COMPLETE
from app.seed.sqlserver import (
    INDEXED_COLUMN_CHARS,
    MAX_INDEX_KEY_BYTES,
    RawTableWriter,
    key_column_chars,
)

MSEG = csv_table("MSEG")


def _sql_server_configured() -> bool:
    url = get_settings().database_url
    return bool(url) and backend_of(url) == MSSQL


needs_sql_server = pytest.mark.skipif(not _sql_server_configured(), reason="needs a SQL Server DATABASE_URL")


# --- Widths ---------------------------------------------------------------------


def test_one_or_two_key_columns_keep_the_established_width() -> None:
    assert key_column_chars(0) == INDEXED_COLUMN_CHARS
    assert key_column_chars(1) == INDEXED_COLUMN_CHARS
    assert key_column_chars(2) == 425


@pytest.mark.parametrize("table", CSV_TABLES, ids=lambda t: t.sap_table)
def test_every_csv_tables_key_fits_the_index_limit_and_its_widest_sap_field(table) -> None:
    spec = spec_for(table.entity_set)
    keys = [sap_field(k) for k in spec.identity_keys]
    declared = {sap_field(p.name): p.max_length for p in spec.entity_set.properties}
    chars = key_column_chars(len(keys))

    assert chars * 2 * len(keys) <= MAX_INDEX_KEY_BYTES
    widest = max((declared.get(k) or 0) for k in keys)
    assert chars > widest, f"{table.sap_table}: {chars} nchars cannot hold SAP's {widest}"


def test_the_writer_bounds_only_the_indexed_columns() -> None:
    cursor = FakeCursor()
    RawTableWriter(indexed_columns=("MBLNR", "MJAHR"), indexed_chars=283).create_table(
        cursor, "raw_x", ["MBLNR", "MJAHR", "SGTXT"]
    )
    assert cursor.statements[-1] == (
        "CREATE TABLE [raw_x] ([MBLNR] nvarchar(283), [MJAHR] nvarchar(283), [SGTXT] nvarchar(max))"
    )


def test_the_writer_default_is_unchanged() -> None:
    cursor = FakeCursor()
    RawTableWriter(indexed_columns=("MATNR",)).create_table(cursor, "raw_x", ["MATNR", "MAKTX"])
    assert cursor.statements[-1] == "CREATE TABLE [raw_x] ([MATNR] nvarchar(450), [MAKTX] nvarchar(max))"


def test_the_key_index_is_one_index_over_the_whole_key_in_key_order() -> None:
    cursor = FakeCursor()
    RawTableWriter().create_key_index(cursor, "raw_mseg", ["MBLNR", "MJAHR", "ZEILE"])
    assert cursor.statements == ["CREATE INDEX [ix_raw_mseg_key] ON [raw_mseg] ([MBLNR], [MJAHR], [ZEILE])"]


# --- Which columns ------------------------------------------------------------------


def test_the_key_is_the_sets_identity_key_spelled_as_the_file_spells_it() -> None:
    columns = ["MANDT", "zeile", "MBLNR", "Mjahr", "MATNR"]
    assert csv_load.key_index_columns(MSEG, columns) == ["MBLNR", "Mjahr", "zeile"]


def test_a_file_missing_a_key_column_gets_no_index() -> None:
    assert csv_load.key_index_columns(MSEG, ["MBLNR", "MJAHR", "MATNR"]) == []


def test_an_unknown_set_gets_no_index(monkeypatch) -> None:
    def unknown(name):
        raise KeyError(name)

    monkeypatch.setattr("app.ingest.manifest.spec_for", unknown)
    assert csv_load.key_index_columns(MSEG, ["MBLNR", "MJAHR", "ZEILE"]) == []


def test_the_load_and_the_increment_agree_on_the_key() -> None:
    for table in CSV_TABLES:
        keys, _ = csv_delta._key_columns(table)
        assert csv_load.key_index_columns(table, keys) == keys


# --- Recognising an overlong key -------------------------------------------------------


class FakeDriverError(Exception):
    """pyodbc's shape: args[0] is the SQLSTATE, args[1] the message."""


TRUNCATED = FakeDriverError(
    "22001",
    "[22001] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]String or binary data "
    "would be truncated in table 'raw_mseg', column 'MBLNR'. (2628)",
)


def test_a_truncation_is_recognised() -> None:
    assert csv_load._too_wide_for_key(TRUNCATED)
    assert csv_load._too_wide_for_key(FakeDriverError("01000", "String or binary data would be truncated."))


def test_a_truncation_wrapped_by_sqlalchemy_is_recognised() -> None:
    assert csv_load._too_wide_for_key(DataError("INSERT ...", None, TRUNCATED))


def test_other_failures_are_not() -> None:
    assert not csv_load._too_wide_for_key(FakeDriverError("08S01", "Communication link failure"))
    assert not csv_load._too_wide_for_key(RuntimeError("boom"))


# --- The load -----------------------------------------------------------------------------


class FakeCursor:
    """Records statements, and refuses an overlong value the way SQL Server does."""

    def __init__(self, fail_index: bool = False, fail_insert: Exception | None = None) -> None:
        self.statements: list[str] = []
        self.rows: list[tuple] = []
        self.widths: list[int | None] = []
        self.fail_index = fail_index
        self.fail_insert = fail_insert
        self.fast_executemany = False

    def execute(self, sql: str, *params) -> None:
        self.statements.append(sql)
        if sql.startswith("CREATE TABLE"):
            body = sql[sql.index("(") + 1 : -1]
            self.widths = [
                int(m.group(1)) if (m := re.search(r"nvarchar\((\d+)\)$", part.strip())) else None
                for part in body.split(", ")
            ]
            self.rows = []
        if sql.startswith("CREATE INDEX") and self.fail_index:
            raise FakeDriverError("42000", "Could not allocate space for object")

    def setinputsizes(self, sizes) -> None:
        pass

    def executemany(self, sql: str, batch) -> None:
        if self.fail_insert is not None:
            raise self.fail_insert
        for row in batch:
            for value, width in zip(row, self.widths):
                if width is not None and value is not None and len(value) > width:
                    raise TRUNCATED
        self.rows.extend(batch)


class FakeEngine:
    def __init__(self, cursor: FakeCursor) -> None:
        self.cursor = cursor
        self.transactions: list[str] = []

    @contextlib.contextmanager
    def begin(self):
        connection = SimpleNamespace(connection=SimpleNamespace(driver_connection=SimpleNamespace(cursor=lambda: self.cursor)))
        try:
            yield connection
        except Exception:
            self.transactions.append("rolled back")
            raise
        self.transactions.append("committed")


class FakeStorage:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.opened = 0

    @contextlib.contextmanager
    def open_read(self, key: str):
        self.opened += 1
        yield io.BytesIO(self.data)


MSEG_HEADER = "MANDT,MBLNR,MJAHR,ZEILE,MATNR,CPUDT_MKPF,SGTXT"


def _mseg_file(*rows: str, header: str = MSEG_HEADER) -> bytes:
    return (header + "\r\n" + "".join(r + "\r\n" for r in rows)).encode()


@pytest.fixture
def load(monkeypatch):
    """Run csv_load.load_table for MSEG over the given file, everything but the cursor faked.

    RAW_KEY_INDEX_ENABLED is on unless the test says ``enabled=False``.
    """
    calls = SimpleNamespace(recorded=[], watermarks=[], rebuilt=[], marked=[])
    monkeypatch.setattr(csv_load, "_record", lambda spec, record, status, rows, started, error: calls.recorded.append((status, rows)))
    monkeypatch.setattr(csv_load, "_seed_watermark", lambda entity_set, field, dats, rows: calls.watermarks.append((dats, rows)) or "2026-10-01")
    monkeypatch.setattr(csv_load, "_mark_loaded", lambda request_id: calls.marked.append(request_id))
    monkeypatch.setattr(csv_load.sap_normalise, "refresh_after_load", lambda table: calls.rebuilt.append(table) or True)
    monkeypatch.setattr(csv_load.sap_system, "ensure", lambda: None)

    def run(data: bytes, cursor: FakeCursor | None = None, *, enabled: bool = True):
        cursor = cursor or FakeCursor()
        engine = FakeEngine(cursor)
        storage = FakeStorage(data)
        monkeypatch.setattr(csv_load, "get_engine", lambda: engine)
        settings = get_settings().model_copy(update={"raw_key_index_enabled": enabled})
        monkeypatch.setattr(csv_load, "get_settings", lambda: settings)
        record = SimpleNamespace(
            request_id="r1", status=STATUS_COMPLETE, data_key="k", reconcile=None,
            error=None, from_date=None, to_date=None,
        )
        result = csv_load.load_table("MSEG", request=record, storage=storage)
        return SimpleNamespace(result=result, cursor=cursor, engine=engine, storage=storage, calls=calls)

    return run


ROWS = (
    "900,4900000001,2026,0001,M1,01.10.2026,first",
    "900,4900000001,2026,0002,M2,02.10.2026,second",
    "900,,2026,0001,M3,03.10.2026,blank key kept by a full load",
)


def test_the_key_columns_are_bounded_and_indexed_after_the_rows(load) -> None:
    run = load(_mseg_file(*ROWS))

    assert run.result.ok, run.result.error
    assert run.result.rows == 3
    drop, create, *_, last = run.cursor.statements
    assert drop == "DROP TABLE IF EXISTS [raw_mseg]"
    assert "[MBLNR] nvarchar(283), [MJAHR] nvarchar(283), [ZEILE] nvarchar(283)" in create
    assert "[MATNR] nvarchar(max)" in create and "[SGTXT] nvarchar(max)" in create
    assert last == "CREATE INDEX [ix_raw_mseg_key] ON [raw_mseg] ([MBLNR], [MJAHR], [ZEILE])"
    assert run.engine.transactions == ["committed"]
    assert run.storage.opened == 1
    assert run.calls.rebuilt == ["mseg"] and run.calls.marked == ["r1"]


def test_a_key_too_wide_for_its_column_loads_again_as_before(load, caplog) -> None:
    overlong = "9" * 300
    run = load(_mseg_file(*ROWS, f"900,{overlong},2026,0003,M4,04.10.2026,garbled"))

    assert run.result.ok, run.result.error
    assert run.result.rows == 4, "counted once, not once per attempt"
    assert run.engine.transactions == ["rolled back", "committed"]
    assert run.storage.opened == 2
    final_create = [s for s in run.cursor.statements if s.startswith("CREATE TABLE")][-1]
    assert "nvarchar(283)" not in final_create, "the fallback is the table exactly as before"
    assert not any(s.startswith("CREATE INDEX") for s in run.cursor.statements[run.cursor.statements.index(final_create):])
    assert run.calls.watermarks == [("20261004", 4)], "the mark is seeded once, from the load that stood"
    assert run.calls.recorded == [("succeeded", 4)]
    assert "loading again without it" in caplog.text


def test_an_index_that_cannot_be_built_loads_again_as_before(load, caplog) -> None:
    run = load(_mseg_file(*ROWS), FakeCursor(fail_index=True))

    assert run.result.ok, run.result.error
    assert run.engine.transactions == ["rolled back", "committed"]
    assert run.cursor.statements[-1].startswith("CREATE TABLE"), "the second attempt builds no index"
    assert "nvarchar(283)" not in run.cursor.statements[-1]
    assert len(run.cursor.rows) == 3
    assert run.calls.recorded == [("succeeded", 3)]
    assert "loading again without it" in caplog.text


def test_any_other_failure_fails_once_as_it_always_did(load) -> None:
    run = load(_mseg_file(*ROWS), FakeCursor(fail_insert=FakeDriverError("08S01", "Communication link failure")))

    assert not run.result.ok
    assert "Communication link failure" in run.result.error
    assert run.engine.transactions == ["rolled back"]
    assert run.storage.opened == 1, "not retried"
    assert run.calls.recorded == [("failed", 0)]
    assert run.calls.rebuilt == []


def test_a_file_without_its_key_columns_loads_exactly_as_before(load) -> None:
    run = load(_mseg_file("900,M1,first", header="MANDT,MATNR,SGTXT"))

    assert run.result.ok, run.result.error
    assert run.cursor.statements[1] == "CREATE TABLE [raw_mseg] ([MANDT] nvarchar(max), [MATNR] nvarchar(max), [SGTXT] nvarchar(max))"
    assert not any(s.startswith("CREATE INDEX") for s in run.cursor.statements)
    assert run.storage.opened == 1


# --- The switch ---------------------------------------------------------------------------


def test_the_index_is_off_unless_switched_on() -> None:
    from app.core.config import Settings

    assert Settings(_env_file=None).raw_key_index_enabled is False


def test_switched_off_a_table_with_its_keys_is_built_exactly_as_before(load) -> None:
    run = load(_mseg_file(*ROWS), enabled=False)

    assert run.result.ok, run.result.error
    assert run.cursor.statements == [
        "DROP TABLE IF EXISTS [raw_mseg]",
        "CREATE TABLE [raw_mseg] ([MANDT] nvarchar(max), [MBLNR] nvarchar(max), [MJAHR] nvarchar(max), "
        "[ZEILE] nvarchar(max), [MATNR] nvarchar(max), [CPUDT_MKPF] nvarchar(max), [SGTXT] nvarchar(max))",
    ]
    assert run.storage.opened == 1
    assert run.engine.transactions == ["committed"]
    assert run.calls.recorded == [("succeeded", 3)]


def test_switched_off_a_failure_is_not_retried(load) -> None:
    overlong = "9" * 300
    run = load(_mseg_file(*ROWS, f"900,{overlong},2026,0003,M4,04.10.2026,long"), enabled=False)

    assert run.result.ok, "no bounded column, so nothing is too wide"
    assert run.storage.opened == 1
    assert run.result.rows == 4


# --- The merges ---------------------------------------------------------------------------


def test_a_staging_column_takes_the_raw_columns_width() -> None:
    assert isinstance(staging_type(NVARCHAR(283)), Unicode)
    assert staging_type(NVARCHAR(283)).length == 283
    assert staging_type(NVARCHAR()) is UnicodeText, "nvarchar(max): every table built before the index"
    assert staging_type(None) is UnicodeText, "a column the table does not have yet"


@pytest.fixture
def sqlite(monkeypatch):
    engine = create_engine("sqlite://", future=True)
    from app.shared import sap_normalise

    monkeypatch.setattr(sap_normalise, "refresh_after_load", lambda table: True)
    return engine


def _spy_staging_types(monkeypatch, module) -> dict[str, object]:
    seen: list[object] = []

    def spy(raw_type):
        chosen = staging_type(raw_type)
        seen.append(chosen)
        return chosen

    monkeypatch.setattr(module, "staging_type", spy)
    return seen


def test_the_odata_merge_into_an_indexed_table_stages_keys_at_their_width(sqlite, monkeypatch) -> None:
    with sqlite.begin() as c:
        c.execute(text(
            "CREATE TABLE raw_ekpo (EBELN VARCHAR(425), EBELP VARCHAR(425), MENGE TEXT, ZZKTWRT TEXT)"))
        c.execute(text("INSERT INTO raw_ekpo VALUES ('4100001503', '00010', '5.000', 'kept')"))
    seen = _spy_staging_types(monkeypatch, raw_merge)

    result = raw_merge.merge(spec_for("PurchaseOrderItemSet"), [
        {"Ebeln": "4100001503", "Ebelp": "00010", "Menge": "7.000"},
        {"Ebeln": "4100001503", "Ebelp": "00020", "Menge": "1.000"},
    ], engine=sqlite)

    assert result.status == raw_merge.MERGED, result.detail
    assert (result.updated, result.inserted) == (1, 1)
    assert sorted(getattr(t, "length", None) or 0 for t in seen)[-2:] == [425, 425]
    with sqlite.connect() as c:
        assert c.execute(text("SELECT MENGE, ZZKTWRT FROM raw_ekpo WHERE EBELP='00010'")).one() == ("7.000", "kept")
        assert c.execute(text("SELECT COUNT(*) FROM raw_ekpo")).scalar() == 2


def test_the_csv_increment_into_an_indexed_table_stages_keys_at_their_width(sqlite, monkeypatch) -> None:
    monkeypatch.delitem(__import__("app.ingest.manifest", fromlist=["DELTAS"]).DELTAS, "ReservationItemSet", raising=False)
    with sqlite.begin() as c:
        c.execute(text("CREATE TABLE raw_resb (MANDT TEXT, RSNUM VARCHAR(425), RSPOS VARCHAR(425), BDMNG TEXT)"))
        c.execute(text("INSERT INTO raw_resb VALUES ('900', '0000000001', '0001', '5,000')"))
    seen = _spy_staging_types(monkeypatch, csv_delta)
    storage = FakeStorage(b'MANDT,RSNUM,RSPOS,BDMNG\r\n900,0000000001,0001,"7,000"\r\n900,0000000002,0001,"1,000"\r\n')

    out = csv_delta.merge_increment(csv_table("RESB"), "k", storage=storage, engine=sqlite)

    assert out.ok, out.error
    assert (out.matched, out.updated, out.inserted) == (1, 1, 1)
    assert [getattr(t, "length", None) for t in seen].count(425) == 2
    with sqlite.connect() as c:
        assert c.execute(text("SELECT BDMNG FROM raw_resb WHERE RSNUM='0000000001'")).scalar() == "7,000"


# --- On SQL Server ---------------------------------------------------------------------------


@pytest.fixture
def sql_server_table():
    """A throwaway table name on the configured SQL Server, dropped afterwards."""
    from app.core.db import get_engine

    name = f"raw_ci_keyindex_{uuid.uuid4().hex[:8]}"
    yield get_engine(), name
    with get_engine().begin() as c:
        c.execute(text(f"DROP TABLE IF EXISTS [{name}]"))


def _raw_cursor(engine):
    connection = engine.raw_connection()
    return connection, connection.cursor()


@needs_sql_server
def test_seven_key_columns_at_their_width_index_without_a_warning_and_take_full_width_keys(sql_server_table) -> None:
    engine, table = sql_server_table
    keys = [f"K{i}" for i in range(7)]
    chars = key_column_chars(len(keys))
    writer = RawTableWriter(indexed_columns=tuple(keys), indexed_chars=chars)
    connection, cursor = _raw_cursor(engine)
    try:
        writer.create_table(cursor, table, [*keys, "TXT"])
        cursor.execute(f"INSERT INTO [{table}] VALUES ({', '.join('?' for _ in range(8))})", *(["x" * chars] * 7), "t")
        writer.create_key_index(cursor, table, keys)
        cursor.execute(f"INSERT INTO [{table}] VALUES ({', '.join('?' for _ in range(8))})", *(["y" * chars] * 7), "t")
        connection.commit()
    finally:
        connection.close()

    with engine.connect() as c:
        indexed = c.execute(text(
            "SELECT COUNT(*) FROM sys.index_columns ic JOIN sys.indexes i "
            "ON i.object_id = ic.object_id AND i.index_id = ic.index_id "
            "WHERE i.name = :name AND ic.is_included_column = 0"), {"name": f"ix_{table}_key"}).scalar()
        assert indexed == 7
    types = {c["name"]: c["type"] for c in inspect(engine).get_columns(table)}
    assert staging_type(types["K0"]).length == chars
    assert staging_type(types["TXT"]) is UnicodeText


@needs_sql_server
def test_an_overlong_key_fails_with_the_error_the_fallback_recognises(sql_server_table) -> None:
    engine, table = sql_server_table
    writer = RawTableWriter(indexed_columns=("K",), indexed_chars=10)
    connection, cursor = _raw_cursor(engine)
    try:
        writer.create_table(cursor, table, ["K", "TXT"])
        with pytest.raises(Exception) as caught:
            cursor.execute(f"INSERT INTO [{table}] VALUES (?, ?)", "x" * 11, "t")
        connection.rollback()
    finally:
        connection.close()
    assert csv_load._too_wide_for_key(caught.value), caught.value

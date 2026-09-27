"""The normalise views (``app.shared.sap_normalise``).

Every I08 and I13 read goes through ``n_<table>``, so a wrong column choice or
a transform that means something different on the two engines is wrong
everywhere at once. The pure tests pin the choices; the database test runs the
generated expressions on whichever engine DATABASE_URL names -- SQL Server in
CI and Azure -- over literals, so it proves the syntax without touching any table.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import mssql, postgresql

from app.core.config import get_settings
from app.core.db import MSSQL, backend_of
from app.shared import sap_normalise
from app.shared.sap_normalise import _Dialect, resolve, view_sql

# Gated on the backend, not just a URL, as in tests/test_db.py: a laptop whose
# DATABASE_URL still names Postgres is refused by app.core.db, not skipped.
needs_db = pytest.mark.skipif(
    backend_of(get_settings().database_url or "") != MSSQL,
    reason="DATABASE_URL does not name an Azure SQL database",
)

MSSQL = _Dialect("mssql", mssql.dialect().identifier_preparer.quote)
POSTGRES = _Dialect("postgresql", postgresql.dialect().identifier_preparer.quote)


def _source(table: str, present: list[str], label: str) -> str | None:
    return {r.label: r.source for r in resolve(table, present)}[label]


# --- Which physical column feeds each label ----------------------------------


def test_a_workbook_table_reads_its_own_labels() -> None:
    assert _source("marc", ["material", "plant", "mrp_type"], "mrp_type") == "mrp_type"


def test_a_csv_table_reads_the_sap_field_behind_each_label() -> None:
    """The failure this layer exists for: CSV loads carry MATNR, not material."""
    present = ["MATNR", "WERKS", "DISMM"]

    assert _source("marc", present, "material") == "MATNR"
    assert _source("marc", present, "plant") == "WERKS"
    assert _source("marc", present, "mrp_type") == "DISMM"


def test_matching_is_case_insensitive_but_keeps_the_real_spelling() -> None:
    """The name is quoted in the view, so it must be the column's exact case."""
    assert _source("marc", ["Matnr"], "material") == "Matnr"


def test_the_label_wins_over_a_sap_field_when_both_exist() -> None:
    assert _source("marc", ["material", "MATNR"], "material") == "material"


def test_the_first_listed_sap_field_wins() -> None:
    """MSEG's posting date is BUDAT_MKPF on S/4 and BUDAT on ECC."""
    assert _source("mseg", ["BUDAT", "BUDAT_MKPF"], "posting_date") == "BUDAT_MKPF"
    assert _source("mseg", ["BUDAT"], "posting_date") == "BUDAT"


def test_an_undelivered_column_is_a_gap_not_a_guess() -> None:
    assert _source("marc", ["MATNR"], "mrp_type") is None


# --- The generated view --------------------------------------------------------


def test_an_undelivered_column_is_null_in_the_view() -> None:
    sql = view_sql("marc", ["MATNR", "WERKS"], MSSQL)

    assert "CAST(NULL AS nvarchar(4000)) AS mrp_type" in sql


def test_a_missing_raw_table_gives_an_empty_view_rather_than_an_error() -> None:
    """EKKO and EKET are not in every delivery; a query over them returns nothing."""
    sql = view_sql("eket", None, MSSQL)

    assert "WHERE 1 = 0" in sql
    assert "FROM raw_eket" not in sql


def test_reserved_words_are_quoted_on_both_engines() -> None:
    """RESB's order number is exposed as "order", which is a keyword everywhere."""
    assert "AS [order]" in view_sql("resb", ["AUFNR"], MSSQL)
    assert 'AS "order"' in view_sql("resb", ["AUFNR"], POSTGRES)


def test_sql_server_rebuilds_in_place_and_postgres_recreates() -> None:
    assert view_sql("marc", ["MATNR"], MSSQL).startswith("CREATE OR ALTER VIEW n_marc AS")
    assert view_sql("marc", ["MATNR"], POSTGRES).startswith("CREATE VIEW n_marc AS")


def test_every_i13_table_has_a_view() -> None:
    """The adapters read these n_ names; a table missing here is an outage there."""
    for table in ("marc", "mard", "mseg", "mkpf", "resb", "ekpo", "ekbe", "eban"):
        assert f"n_{table}" in sap_normalise.VIEWS


def test_a_load_of_an_uncovered_table_rebuilds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(sap_normalise, "rebuild_read_layers", lambda: calls.append(1) or True)

    assert sap_normalise.refresh_after_load("zmm065_bmm") is True
    assert calls == []

    assert sap_normalise.refresh_after_load("MSEG") is True
    assert calls == [1]


# --- Number format: declared by vocabulary -----------------------------------


def test_a_csv_number_is_converted_from_german_and_a_workbook_one_is_not() -> None:
    """The CSV writes "0,989" for 0.989; the workbook writes "1,000" for one
    thousand. Which applies is decided by the column the label resolved through."""
    csv_view = view_sql("mseg", ["MATNR", "WERKS", "MENGE"], MSSQL)
    workbook_view = view_sql("mseg", ["material", "plant", "quantity"], MSSQL)

    assert "REPLACE(REPLACE(TRIM([MENGE]), '.', ''), ',', '.')" in csv_view
    assert "REPLACE" not in workbook_view


def test_german_conversion_is_only_applied_to_numbers() -> None:
    """A material number or a date from the CSV must never have its dots or
    commas touched."""
    sql = view_sql("mseg", ["MATNR", "WERKS", "BUDAT_MKPF", "MENGE"], POSTGRES)

    # Every REPLACE is over MENGE: the num expression names it several times,
    # the key and date expressions never call REPLACE at all.
    assert "REPLACE(" in sql
    assert all(chunk.startswith('TRIM("MENGE")') for chunk in sql.split("REPLACE(REPLACE(")[1:])


@pytest.mark.parametrize(
    ("table", "present", "label", "expected"),
    [
        ("mara", ["MATNR", "MSTAE", "MFRNR", "LVORM"], "x_plant_matl_status", "MSTAE"),
        ("mara", ["material", "x_plant_matl_status"], "x_plant_matl_status", "x_plant_matl_status"),
        ("marc", ["MATNR", "WERKS", "MABST", "LVORM"], "maximum_stock_level", "MABST"),
        ("mard", ["MATNR", "WERKS", "LGORT", "UMLME", "EINME"], "restricted_use_stock", "EINME"),
        ("mbew", ["MATNR", "BWKEY", "VERPR"], "moving_price", "VERPR"),
        # The live CSV MBEW has no VERPR: a gap, never a guess.
        ("mbew", ["MANDT", "MATNR", "BWKEY", "BWTAR", "LVORM"], "moving_price", None),
    ],
)
def test_the_columns_i7_reads_resolve_in_both_vocabularies(table, present, label, expected) -> None:
    assert _source(table, present, label) == expected


def test_the_live_csv_marc_leaves_the_mrp_fields_as_gaps() -> None:
    """Measured on Azure, 27 Sep: 27 columns, none of them MRP fields. I07
    fills these from odata_material_plant; the view must not invent them."""
    live = ["MANDT", "MATNR", "WERKS", "column_4", "UMLMC", "TRAME", "ZZCRITIC"]
    gaps = {r.label for r in resolve("marc", live) if r.source is None}

    assert {"mrp_type", "planned_deliv_time", "reorder_point", "maximum_stock_level"} <= gaps


# --- The transforms, on the configured engine ---------------------------------


@needs_db
@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    [
        ("key", "0000219937", "219937"),
        ("key", "  00012 ", "12"),
        ("key", "SPARE-12", "SPARE-12"),
        ("date", "2026-07-14", "2026-07-14"),
        ("date", "14.07.2026", "2026-07-14"),
        ("date", "20260714", "2026-07-14"),
        ("date", "00000000", None),
        ("date", "00.00.0000", None),
        ("date", "not a date", "not a date"),
        ("num", "12.000-", "-12.000"),
        ("num", "5.5", "5.5"),
    ],
)
def test_the_transform_means_the_same_on_this_engine(kind: str, raw: str, expected: str | None) -> None:
    from app.core.db import get_engine

    engine = get_engine()
    dialect = sap_normalise._dialect(engine)
    expression = getattr(dialect, kind)(f"'{raw}'")

    with engine.connect() as connection:
        value = connection.execute(text(f"SELECT {expression}")).scalar()

    assert value == expected


@needs_db
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0,989", "0.989"),  # raw_mseg.MENGE, live
        ("1,000", "1.000"),
        ("1.234,5", "1234.5"),
        ("12.345.678,9", "12345678.9"),
        ("5,000-", "-5.000"),
        ("0", "0"),
    ],
)
def test_the_german_number_transform_on_this_engine(raw: str, expected: str) -> None:
    from app.core.db import get_engine

    engine = get_engine()
    expression = sap_normalise._dialect(engine).num(f"'{raw}'", german=True)

    with engine.connect() as connection:
        value = connection.execute(text(f"SELECT {expression}")).scalar()

    assert value == expected


# --- The SQL Server append-only triggers ---------------------------------------


def _migration(name: str):
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sql_server_protects_the_same_tables_with_the_same_messages_as_postgres() -> None:
    """Two engines, one guarantee: a table protected on one must be on both."""
    postgres = _migration("d8c3b1f04e75_assistant_spine.py")
    sql_server = _migration("5c9e2a7d4f18_assistant_immutability_mssql.py")

    assert sql_server._APPEND_ONLY == postgres._APPEND_ONLY


def test_the_sql_server_trigger_blocks_update_and_delete_by_the_postgres_name() -> None:
    sql_server = _migration("5c9e2a7d4f18_assistant_immutability_mssql.py")

    sql = sql_server._trigger("assistant_turn", "it's evidence")

    assert "CREATE TRIGGER assistant_turn_no_update_or_delete" in sql
    assert "INSTEAD OF UPDATE, DELETE" in sql
    assert "it''s evidence" in sql  # a quote in the guidance cannot end the literal

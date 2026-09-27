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

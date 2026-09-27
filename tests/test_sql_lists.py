"""Portable list predicates (``app.shared.sql_lists``) and the I08 queries on them.

These replaced Postgres's ``= any(:array)`` and ``like any(:array)`` so I08 runs
on Azure SQL. The pure tests pin the behaviour; the database test runs the
generated LIKE on the configured engine -- SQL Server in CI.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import MSSQL, backend_of
from app.initiatives.i8 import coding_candidates
from app.initiatives.i8.config import I8Settings
from app.shared.sql_lists import (
    IN_CHUNK,
    chunked,
    fetch_in_chunks,
    like_any,
    like_literal,
)

needs_azure_sql = pytest.mark.skipif(
    backend_of(get_settings().database_url or "") != MSSQL,
    reason="DATABASE_URL does not name an Azure SQL database",
)


# --- chunking -----------------------------------------------------------------


def test_chunks_stay_under_the_sql_server_parameter_limit() -> None:
    """SQL Server refuses a statement with more than 2,100 parameters."""
    chunks = list(chunked(range(2500)))

    assert [len(c) for c in chunks] == [IN_CHUNK, IN_CHUNK, 500]
    assert max(len(c) for c in chunks) < 2100
    assert [v for c in chunks for v in c] == list(range(2500))


def test_an_empty_list_is_no_chunks() -> None:
    assert list(chunked([])) == []


class _Result:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict]:
        return self._rows

    def scalars(self) -> list:
        return [next(iter(r.values())) for r in self._rows]


class _FakeDb:
    """Answers ``... in :name`` from a list of rows, recording every call."""

    def __init__(self, rows: list[dict], key: str, distinct: list | None = None) -> None:
        self.rows, self.key, self.distinct = rows, key, distinct
        self.calls: list[dict[str, Any]] = []

    def execute(self, statement, params: dict | None = None) -> _Result:
        self.calls.append(params or {})
        if params is None:
            return _Result([{"matnr": m} for m in self.distinct or []])
        wanted = set(next(v for v in params.values() if isinstance(v, list)))
        return _Result([r for r in self.rows if r[self.key] in wanted])


def test_fetch_in_chunks_returns_every_row_and_passes_the_other_params() -> None:
    rows = [{"ebeln": str(n)} for n in range(1500)]
    db = _FakeDb(rows, "ebeln")

    found = fetch_in_chunks(db, "select 1 where ebeln in :documents", "documents",
                            [r["ebeln"] for r in rows], {"category": "E"})

    assert found == rows
    assert len(db.calls) == 2
    assert all(call["category"] == "E" for call in db.calls)


def test_fetch_in_chunks_with_nothing_to_ask_runs_nothing() -> None:
    db = _FakeDb([], "ebeln")

    assert fetch_in_chunks(db, "select 1 where ebeln in :d", "d", []) == []
    assert db.calls == []


# --- LIKE -----------------------------------------------------------------------


def test_every_like_wildcard_is_escaped() -> None:
    assert like_literal("50%_off[1]\\") == "50\\%\\_off\\[1]\\\\"


def test_like_any_ors_one_bind_parameter_per_pattern() -> None:
    sql, params = like_any("matnr", "pattern", ["80%", "81%"])

    assert sql == "(matnr like :pattern_0 escape '\\' or matnr like :pattern_1 escape '\\')"
    assert params == {"pattern_0": "80%", "pattern_1": "81%"}


def test_no_patterns_matches_nothing_rather_than_breaking_the_query() -> None:
    assert like_any("matnr", "pattern", []) == ("1 = 0", {})


@needs_azure_sql
@pytest.mark.parametrize(
    ("value", "keyword", "matches"),
    [
        ("Pump REPAIRED at vendor", "repair", True),
        ("pump repaired at vendor", "repair", True),
        ("new pump", "repair", False),
        ("50% discount", "50%", True),
        ("500 units", "50%", False),
        ("a_b", "a_b", True),
        ("axb", "a_b", False),
        ("[x] seal", "[x]", True),
        ("x seal", "[x]", False),
    ],
)
def test_the_keyword_match_means_the_same_on_this_engine(value: str, keyword: str, matches: bool) -> None:
    """Case-insensitive substring, wildcards literal -- what matched_keywords does in Python."""
    from app.core.db import get_engine

    cfg = I8Settings(repair_language=keyword, _env_file=None)
    predicate, params = like_any("upper(:value)", "keyword", cfg.repair_language_like_patterns)

    with get_engine().connect() as connection:
        hit = connection.execute(
            text(f"select case when {predicate} then 1 else 0 end"), {"value": value, **params}
        ).scalar()

    assert bool(hit) is matches
    assert bool(coding_candidates.matched_keywords(value, cfg)) is matches


# --- the I08 twin check, now matched in Python ------------------------------------


def test_texts_are_compared_upper_cased_and_whitespace_collapsed() -> None:
    assert coding_candidates.normalise_text("  Refurbished \t LINCOLN  ln-25 ") == "REFURBISHED LINCOLN LN-25"


def test_a_suspect_sharing_an_80_series_text_is_a_twin() -> None:
    """The same answer the old SQL gave: per suspect, each coded material with
    the same normalised text, and that coded material's first raw text."""
    cfg = I8Settings(_env_file=None)
    coded, suspect, unrelated = "8000000001", "1234567", "7654321"
    rows = [
        {"matnr": coded, "txz01": "REFURBISHED  LINCOLN LN-25"},
        {"matnr": coded, "txz01": "Refurbished Lincoln LN-25"},
        {"matnr": suspect, "txz01": "refurbished lincoln ln-25"},
        {"matnr": unrelated, "txz01": "Something else"},
    ]
    db = _FakeDb(rows, "matnr", distinct=[coded, suspect, unrelated])

    twins = coding_candidates.find_twins(db, [suspect, unrelated], cfg)

    assert twins == {suspect: ((coded, "REFURBISHED  LINCOLN LN-25"),)}

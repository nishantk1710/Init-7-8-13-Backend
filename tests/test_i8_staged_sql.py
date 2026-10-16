"""The staged I08 build's SQL calls no helper function -- and inlines them faithfully.

On Azure the API's database identity may SELECT from the I08 views but not
EXECUTE the helpers they call: ownership chaining covers a call made inside a
view, not one made directly. The staged build runs each view's body directly,
so it writes the helpers inline (``staged.INLINE_HELPERS``). These tests need
no database:

* every staged view body is left with no ``dbo.<function>(`` call at all;
* each inline form IS the function's body, read from 00_functions.mssql.sql,
  so changing one without the other fails here;
* a call the inliner cannot handle is an error, not a silent pass-through.

``test_i8_staged_build.py`` then proves on SQL Server that the staged build
returns what the views return, and ``test_i8_coding_staged.py`` does the same
for the FR-2 coding screen.
"""

from __future__ import annotations

import re

import pytest

from app.initiatives.i8 import staged
from app.initiatives.i8.views import SQL_DIR


def _normal(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


def _function_bodies() -> dict[str, str]:
    script = (SQL_DIR / "00_functions.mssql.sql").read_text(encoding="utf-8")
    script = re.sub(r"--[^\n]*", "", script)
    bodies = {}
    for chunk in re.split(r"(?i)\bcreate\s+or\s+alter\s+function\b", script)[1:]:
        name = re.match(r"\s*dbo\.(\w+)", chunk).group(1).lower()
        after_return = re.split(r"(?i)\breturn\b", chunk, maxsplit=2)[-1]
        bodies[name] = re.sub(r"(?is)\bend\s*$", "", after_return.strip()).strip()
    return bodies


def test_every_managed_view_has_a_body_to_stage() -> None:
    """``_FILES`` is how staging finds a view's SQL. A view missing from it is a
    ``KeyError`` on the first staged read -- caught by CI on 13-Oct-2026 for
    ``v_eban``, which would otherwise have been a 500 on every request."""
    from app.initiatives.i8.views import VIEWS

    assert set(staged._FILES) == set(VIEWS)
    for view, script in staged._FILES.items():
        assert (SQL_DIR / script).exists(), f"{view}: no {script}"


@pytest.mark.parametrize("view", [*staged.VIEWS, "v_eban", "v_mbew"])
def test_no_staged_view_calls_a_helper(view) -> None:
    body = staged.inline_helpers(staged._body(view))
    assert not re.search(r"(?i)\bdbo\.\w+\(", body), body


@pytest.mark.parametrize("name", sorted(staged.INLINE_HELPERS))
def test_each_inline_form_is_the_function_body(name) -> None:
    bodies = _function_bodies()
    assert name in bodies, f"no {name} in 00_functions.mssql.sql (found {sorted(bodies)})"
    assert _normal(staged.INLINE_HELPERS[name].format(v="@value")) == _normal(bodies[name])


def test_in_scope_plant_is_inlined_with_the_module_plants() -> None:
    from app.shared.plant_scope import sql_literals

    inlined = staged.inline_helpers("where dbo.in_scope_plant(plant) = 1")
    assert inlined == f"where (TRIM(COALESCE(plant, '')) IN ({sql_literals()}))"


def test_a_call_it_cannot_inline_is_refused() -> None:
    with pytest.raises(ValueError, match="could not be inlined"):
        staged.inline_helpers("select dbo.sap_key(nullif(material, '')) from n_mara")


class TestTheCodingNarrowingIsASuperset:
    """The FR-2 screen's staging keeps every row its own predicate would match.

    Staging is only safe if the narrowing is a SUPERSET of the query that runs
    over it. For the coding screen the query is
    ``upper(<view text>) LIKE '%STEM%' ESCAPE`` per configured stem, and the
    narrowing is the same LIKE over the raw column the view reads. These pin
    that they stay the same shape; ``test_i8_coding_staged.py`` proves the
    rows agree on a real database.
    """

    def test_every_configured_stem_appears_in_the_narrowing(self) -> None:
        from app.initiatives.i8.config import get_i8_settings

        cfg = get_i8_settings()
        sql = staged._mentions("short_text", cfg)
        for stem in cfg.repair_language_list:
            assert stem.upper() in sql, f"{stem!r} would be staged away before the screen saw it"

    def test_it_matches_on_upper_like_the_screen_does(self) -> None:
        from app.initiatives.i8.config import get_i8_settings

        # The screen's text_column is upper(...); a narrowing that compared raw
        # text would drop every lower-case hit before the screen ran.
        sql = staged._mentions("short_text", get_i8_settings())
        assert "UPPER(short_text)" in sql

    def test_no_stems_keeps_nothing_rather_than_everything(self) -> None:
        """``like_any`` renders ``1 = 0`` for no patterns -- the screen matches
        nothing -- so the narrowing must agree rather than staging the lot."""

        class NoStems:
            repair_language_list = ()

        assert staged._mentions("short_text", NoStems()) == "1 = 0"

    def test_a_stem_with_a_wildcard_is_escaped(self) -> None:
        """A keyword containing % or _ must narrow the search, not become a
        wildcard -- the same rule config.repair_language_like_patterns keeps."""

        class Odd:
            repair_language_list = ("re%air",)

        sql = staged._mentions("short_text", Odd())
        assert r"RE\%AIR" in sql and "ESCAPE" in sql


class TestTheCodingDenominator:
    """The denominator counts the source, never the narrowed copy."""

    def test_it_reads_the_normalised_table_not_a_staged_one(self) -> None:
        for source, (_, table, _) in staged.CODING_SOURCES.items():
            sql = staged.plain_text_total(source)
            assert f"FROM {table}" in sql, source
            assert "#i8" not in sql, f"{source}: the denominator must not read a staged copy"

    def test_it_applies_the_view_s_own_predicates(self) -> None:
        """The view is ``nullif(<raw>, '')`` over in-scope plants; counting the
        table without both would not be the same number."""
        for source, (_, _, column) in staged.CODING_SOURCES.items():
            sql = staged.plain_text_total(source)
            assert f"NULLIF({column}, '') IS NOT NULL" in sql, source
            assert "plant" in sql, source

    def test_it_calls_no_helper_function(self) -> None:
        for source in staged.CODING_SOURCES:
            assert not re.search(r"(?i)dbo\.\w+\(", staged.plain_text_total(source))

    def test_an_unknown_source_has_no_plain_count(self) -> None:
        assert staged.plain_text_total("NOT_A_SOURCE") is None

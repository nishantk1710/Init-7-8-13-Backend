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
returns what the views return.
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


@pytest.mark.parametrize("view", staged.VIEWS)
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

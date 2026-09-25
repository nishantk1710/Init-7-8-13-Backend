"""The plants this platform is in scope for.

Two, and only two
-----------------
The team lead's ruling of 2026-09-21 sets the delivery scope at **1300 (Black
Mountain Mining) and 1500 (Gamsberg)**. Nothing else is served, counted or
displayed.

This reverses the earlier "keep all of them, keyed by code" decision recorded
in ``docs/Initiative_08_Decisions_21Sep.md``. Under that ruling the readers
were deliberately plant-agnostic -- they filtered on whatever code they were
handed and hard-coded nothing -- so the July extract's other four codes (1200,
1600, 2000 and 3000) flowed straight through to the API. They no longer do.

Why a filter had to be added rather than removed
-------------------------------------------------
"Only two plants" is not a smaller version of "any plant". The previous ruling
needed no code because *serving whatever arrives* is the default behaviour of
a query that takes a plant parameter. Restricting the set is the thing that
needs code, and it needs it at the point data is read rather than at the point
it is rendered: a total, an aging band or a reconciliation percentage computed
over six plants is wrong even when the row that carried the sixth is never
drawn on screen.

One list, two enforcement points
---------------------------------
Every read reaches Postgres through one of two paths, and both are filtered
from the constant below:

- **The I08 views** (``app/initiatives/i8/sql/``) filter through the
  ``in_scope_plant()`` SQL function, which ``ensure_views`` generates from
  ``IN_SCOPE_PLANTS`` so the database cannot drift from this module.
- **The I13 adapters** (``app/integrations/sap/postgres_*.py``) compose
  ``sql_predicate()`` into their WHERE clauses.

An out-of-scope code passed to an API's ``?plant=`` parameter matches nothing
and returns an empty result. That is the correct answer, not a gap -- the same
distinction ``app.shared.numbers`` draws between "no source told us" and
"zero".
"""

from __future__ import annotations

#: The in-scope plant codes, as SAP carries them. Ordered as the business
#: names them (Black Mountain first) so any listing built from this reads the
#: way the reviewer expects.
#:
#: A tuple rather than a set: the order is load-bearing for display, and
#: immutability means a caller cannot widen the platform's scope by accident.
IN_SCOPE_PLANTS: tuple[str, ...] = ("1300", "1500")

#: Plant code -> the name used in the delivery. Both are documented facts from
#: the extract (see ``app/seed/manifest.py``), not invented labels.
#:
#: ``I8_PLANT_NAMES`` overrides this for I08's own rendering; this mapping is
#: the fallback every other caller shares.
PLANT_NAMES: dict[str, str] = {
    "1300": "Black Mountain Mining",
    "1500": "Gamsberg",
}


def normalise(plant: str | None) -> str | None:
    """A plant code trimmed to the form the constants use, or ``None``.

    Blank and whitespace-only become ``None`` rather than ``""``, matching how
    the views treat an empty cell -- so "no plant on this row" and "a plant
    that is out of scope" stay distinguishable to the caller.
    """
    if plant is None:
        return None
    return (plant or "").strip() or None


def is_in_scope(plant: str | None) -> bool:
    """Whether a plant code is one of the two the platform serves.

    ``None`` and blank are **not** in scope. A row with no plant cannot be
    attributed to either site, and guessing one would put a quantity under a
    heading no evidence supports.
    """
    return normalise(plant) in IN_SCOPE_PLANTS


def plant_name(plant: str | None) -> str | None:
    """The display name for a plant code, falling back to the code itself.

    Never invents a name: an unrecognised code is returned unchanged, which is
    a fact, where a made-up site name would not be.
    """
    code = normalise(plant)
    if code is None:
        return None
    return PLANT_NAMES.get(code, code)


def sql_literals() -> str:
    """The in-scope codes as a SQL ``IN`` list, e.g. ``'1300', '1500'``.

    Safe to interpolate: the values come from this module, never from a
    request. Built here rather than written out at each call site so adding a
    third plant one day is a one-line change in this file.
    """
    return ", ".join(f"'{code}'" for code in IN_SCOPE_PLANTS)


def sql_predicate(column: str) -> str:
    """A WHERE fragment restricting ``column`` to the in-scope plants.

    ``column`` is a caller-supplied identifier (``plant``, ``m.plant``), never
    user input. Trimmed before comparison because the raw extract layer is all
    text and a padded cell would otherwise fall out of scope silently.
    """
    return f"btrim({column}) IN ({sql_literals()})"

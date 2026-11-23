"""Every active material at the in-scope plants, with its stock on hand.

The More -> Materials page (23 Nov 2026): one row per material and plant, for
every material type -- not only OAR.

The rule
--------
- **Rows**: MARC (``n_marc``), one per material + plant, in-scope plants only,
  minus anything flagged for deletion -- MARC-LVORM (at that plant) or
  MARA-LVORM (at client level, so every plant) equal to ``X``. Read through
  :func:`fetch_material_scope_index`, the same read the OAR population comes
  from, so the OAR rows here are exactly the OAR positions I13 counts.
- **OAR / Non-OAR**: :func:`classify_material_scope` on MARC-DISMM -- OAR when
  it is one of ``Settings.i13_oar_mrp_types`` (ND, PD), Non-OAR otherwise.
- **Stock on hand**: MARD-LABST summed over the storage locations of each
  material + plant -- :func:`fetch_current_stock`, the figure Usage patterns and
  Non-movers show. A pair with no MARD row (never received at that plant, so
  no storage location yet) has nothing on hand and reads 0.
- **Description**: :mod:`app.shared.material_names` (MAKT English first, then
  ZMM065), looked up per request because that list loads in the background.
- **Unit**: MARA-MEINS.

Held in memory
--------------
On Azure SQL the ``n_*`` tables are views that normalise the material number,
so filtering or paging them in SQL cannot use an index (see
``app.shared.material_names``). The whole list is a few tens of thousands of
short rows, so it is read once -- three queries -- and every request filters,
counts and pages it in Python. It is rebuilt after :data:`CACHE_SECONDS`, so a
new SAP load shows within that time.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.assistant.material_search import parse_query
from app.initiatives.i8.material_number import normalise
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_movements import fetch_current_stock
from app.shared.material_names import material_names
from app.shared.material_scope import MaterialScope, classify_material_scope
from app.shared.plant_scope import plant_name

#: How long a built list is served before it is read again.
CACHE_SECONDS = 10 * 60

ZERO = Decimal(0)

_UNIT_QUERY = """
    SELECT material, base_unit_of_measure
    FROM n_mara
    WHERE material <> '' AND base_unit_of_measure <> ''
"""


class Classification(str, Enum):
    OAR = "OAR"
    NON_OAR = "NON_OAR"


@dataclass(frozen=True)
class MaterialStock:
    material: str
    plant: str
    classification: Classification
    stock_on_hand: Decimal
    unit: str | None

    @property
    def plant_name(self) -> str | None:
        return plant_name(self.plant)


@dataclass(frozen=True)
class MaterialList:
    rows: tuple[MaterialStock, ...]
    loaded_at: datetime


@dataclass(frozen=True)
class Filters:
    search: str | None = None
    plant: str | None = None
    classification: Classification | None = None


@dataclass(frozen=True)
class Counts:
    total: int
    oar: int
    non_oar: int


_lock = threading.Lock()
_cached: MaterialList | None = None
_cached_until = 0.0


def build(db: Session) -> MaterialList:
    """Read MARC, MARD and MARA once and join them per material + plant."""
    scope = fetch_material_scope_index(db)
    stock = fetch_current_stock(db)
    units = _units(db)
    rows = [
        MaterialStock(
            material=material,
            plant=plant,
            classification=(
                Classification.OAR
                if classify_material_scope(mrp_type) is MaterialScope.OAR
                else Classification.NON_OAR
            ),
            stock_on_hand=stock.get((material, plant), ZERO),
            unit=units.get(material),
        )
        for (material, plant), mrp_type in scope.items()
    ]
    rows.sort(key=lambda r: (r.material, r.plant))
    return MaterialList(rows=tuple(rows), loaded_at=datetime.now(UTC))


def current(db: Session) -> MaterialList:
    """The cached list, rebuilt when it is missing or older than
    :data:`CACHE_SECONDS`. One request builds while the others wait for it,
    rather than all reading the same three tables at once."""
    global _cached, _cached_until
    with _lock:
        if _cached is None or time.monotonic() >= _cached_until:
            _cached = build(db)
            _cached_until = time.monotonic() + CACHE_SECONDS
        return _cached


def clear_cache() -> None:
    global _cached, _cached_until
    with _lock:
        _cached = None
        _cached_until = 0.0


def descriptions(rows: Iterable[MaterialStock]) -> dict[str, str]:
    """``{normalised material: name}`` for the rows a source names."""
    return material_names(r.material for r in rows)


def description_of(row: MaterialStock, names: dict[str, str]) -> str | None:
    key = normalise(row.material)
    return names.get(key) if key else None


def matching(rows: Iterable[MaterialStock], filters: Filters) -> list[MaterialStock]:
    """The rows every filter accepts, in the list's order (material, plant).

    Search reads like the assistant's material search: all digits is a
    material-number prefix (leading zeros ignored), anything else is words
    that must all appear in the description or the number.
    """
    rows = list(rows)
    if filters.plant:
        wanted = filters.plant.strip()
        rows = [r for r in rows if r.plant == wanted]
    if filters.classification is not None:
        rows = [r for r in rows if r.classification is filters.classification]
    return _searched(rows, filters.search)


def counts(rows: Iterable[MaterialStock], filters: Filters) -> Counts:
    """Totals for the OAR / Non-OAR choice: every filter but classification."""
    found = matching(rows, Filters(search=filters.search, plant=filters.plant))
    oar = sum(1 for r in found if r.classification is Classification.OAR)
    return Counts(total=len(found), oar=oar, non_oar=len(found) - oar)


def _searched(rows: list[MaterialStock], search: str | None) -> list[MaterialStock]:
    query = parse_query(search)
    if query.empty:
        return rows
    if query.number_prefix is not None:
        prefix = query.number_prefix
        return [r for r in rows if (normalise(r.material) or "").startswith(prefix)]
    names = descriptions(rows)
    found = []
    for row in rows:
        haystack = f"{row.material} {description_of(row, names) or ''}".upper()
        if all(word in haystack for word in query.words):
            found.append(row)
    return found


def _units(db: Session) -> dict[str, str]:
    """MARA-MEINS per material; the first non-blank one if MARA repeats."""
    units: dict[str, str] = {}
    for r in db.execute(text(_UNIT_QUERY)).fetchall():
        material = (r.material or "").strip()
        unit = (r.base_unit_of_measure or "").strip()
        if material and unit and material not in units:
            units[material] = unit
    return units

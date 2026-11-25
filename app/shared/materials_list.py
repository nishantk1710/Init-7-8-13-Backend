"""Every active material at the in-scope plants, with its stock on hand.

The More -> Materials page (23 Nov 2026): one row per material and plant, for
every material type -- not only OAR.

The rule
--------
- **Rows**: MARC (``n_marc``), one per material + plant, in-scope plants only,
  minus anything flagged for deletion -- MARC-LVORM (at that plant) or
  MARA-LVORM (at client level, so every plant) equal to ``X``: the rule of
  :mod:`app.shared.material_scope.deletion`, so the OAR rows here are the OAR
  positions I13 counts. It is applied here in Python over two plain reads of
  MARC and MARA, not through :func:`fetch_material_scope_index`: that one tests
  every MARC row with a correlated EXISTS over the ``n_mara`` view, which is
  fine for one material but did not finish for the whole of MARC on non-prod
  (25 Nov 2026) and hung over an hour on prod (19 Nov).
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
short rows, so it is read once -- three plain queries -- and every request
filters, counts and pages it in Python.

The read runs in a background thread, never in a request: a request that
finds no list yet gets :class:`NotLoaded` (the API answers 503 "still
loading") instead of holding a server worker while SAP is read. After
:data:`CACHE_SECONDS` the next request starts a fresh read and keeps being
served the previous list until it lands, so a new SAP load shows within about
that time.
"""

from __future__ import annotations

import logging
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
from app.core.db import statement_timeout
from app.initiatives.i8.material_number import normalise
from app.integrations.sap.postgres_movements import fetch_current_stock
from app.shared.material_names import material_names
from app.shared.material_scope import MaterialScope, classify_material_scope
from app.shared.material_scope.deletion import DELETION_FLAG, exclude_deleted
from app.shared.plant_scope import plant_name, sql_predicate

logger = logging.getLogger(__name__)

#: How long a built list is served before it is read again.
CACHE_SECONDS = 10 * 60

#: After a failed read, how long before a request may start another.
RETRY_SECONDS = 60

#: Longest wait for any one query's first row; a read stuck past it fails
#: (logged) rather than leaving the page "loading" until a restart.
QUERY_TIMEOUT_SECONDS = 10 * 60

ZERO = Decimal(0)

_MARC_QUERY = f"""
    SELECT material, plant, mrp_type, df_at_plant_level
    FROM n_marc
    WHERE material <> '' AND plant <> '' AND {sql_predicate("plant")}
"""

_MARA_QUERY = """
    SELECT material, df_at_client_level, base_unit_of_measure
    FROM n_mara
    WHERE material <> ''
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


class NotLoaded(Exception):
    """No list has been read yet. ``failed`` when the last read failed (and a
    new one starts after :data:`RETRY_SECONDS`) rather than still running."""

    def __init__(self, *, failed: bool) -> None:
        super().__init__("materials list not loaded yet")
        self.failed = failed


@dataclass(frozen=True)
class _Material:
    """MARA, per material: client-level deletion and the base unit."""

    deleted: bool
    unit: str | None


_lock = threading.Lock()
_cached: MaterialList | None = None
_next_read_at = 0.0
_reading: threading.Thread | None = None
_last_failed = False


def build(db: Session) -> MaterialList:
    """Read MARC, MARA and MARD once and join them per material + plant."""
    started = time.monotonic()
    with statement_timeout(db, QUERY_TIMEOUT_SECONDS):
        marc = db.execute(text(_MARC_QUERY)).fetchall()
        read_marc = time.monotonic()
        mara = _mara(db)
        read_mara = time.monotonic()
        stock = fetch_current_stock(db)
    read_mard = time.monotonic()

    skip_deleted = exclude_deleted()
    rows: dict[tuple[str, str], MaterialStock] = {}
    for r in marc:
        material, plant = (r.material or "").strip(), (r.plant or "").strip()
        if not material or not plant or (material, plant) in rows:
            continue
        held = mara.get(material)
        if skip_deleted and (_flagged(r.df_at_plant_level) or (held is not None and held.deleted)):
            continue
        rows[material, plant] = MaterialStock(
            material=material,
            plant=plant,
            classification=(
                Classification.OAR
                if classify_material_scope((r.mrp_type or "").strip()) is MaterialScope.OAR
                else Classification.NON_OAR
            ),
            stock_on_hand=stock.get((material, plant), ZERO),
            unit=held.unit if held is not None else None,
        )
    ordered = sorted(rows.values(), key=lambda r: (r.material, r.plant))
    logger.info(
        "materials list: %d rows; MARC %.1fs (%d rows), MARA %.1fs (%d), MARD %.1fs",
        len(ordered), read_marc - started, len(marc), read_mara - read_marc, len(mara), read_mard - read_mara,
    )
    return MaterialList(rows=tuple(ordered), loaded_at=datetime.now(UTC))


def current() -> MaterialList:
    """The list being served. Starts a background read when there is none yet
    or it is older than :data:`CACHE_SECONDS`; never reads SAP itself.

    Raises :class:`NotLoaded` until the first read lands.
    """
    global _reading
    thread = None
    with _lock:
        # _next_read_at is 0 before the first read, CACHE_SECONDS after a
        # good one and RETRY_SECONDS after a failed one.
        if _reading is None and time.monotonic() >= _next_read_at:
            thread = _reading = threading.Thread(target=_read, name="materials-list", daemon=True)
    if thread is not None:
        _start(thread)
    with _lock:
        if _cached is None:
            raise NotLoaded(failed=_last_failed and _reading is None)
        return _cached


def _start(thread: threading.Thread) -> None:
    """Starts the reading thread. Tests replace it to read inline."""
    thread.start()


def _session() -> Session:
    # Imported here: the session factory reads settings, which tests and
    # scripts may set after this module is imported.
    from app.core.db import get_sessionmaker

    return get_sessionmaker()()


def _read() -> None:
    global _cached, _next_read_at, _reading, _last_failed
    started = time.monotonic()
    try:
        with _session() as db:
            loaded = build(db)
        with _lock:
            _cached = loaded
            _next_read_at = time.monotonic() + CACHE_SECONDS
            _last_failed = False
        logger.info("materials list: read in %.1fs", time.monotonic() - started)
    except Exception:  # a failed read must never reach a request
        logger.exception("materials list: read failed after %.1fs", time.monotonic() - started)
        with _lock:
            # The previous list (if any) stays served; try again shortly.
            _next_read_at = time.monotonic() + RETRY_SECONDS
            _last_failed = True
    finally:
        with _lock:
            _reading = None


def wait_for_read(timeout: float | None = None) -> None:
    """Block until a running read ends. For tests and scripts."""
    with _lock:
        reading = _reading
    if reading is not None:
        reading.join(timeout)


def clear_cache() -> None:
    """Forget the list and any read in flight. For tests."""
    global _cached, _next_read_at, _last_failed, _reading
    with _lock:
        _cached = None
        _next_read_at = 0.0
        _last_failed = False
        _reading = None


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


def _flagged(raw: str | None) -> bool:
    """LVORM read the way ``deletion._flag`` reads it in SQL."""
    return (raw or "").strip().upper() == DELETION_FLAG


def _mara(db: Session) -> dict[str, _Material]:
    """Per material: deleted when any MARA row for it is flagged (the EXISTS
    of the shared rule), unit = the first non-blank MEINS."""
    held: dict[str, _Material] = {}
    for r in db.execute(text(_MARA_QUERY)).fetchall():
        material = (r.material or "").strip()
        if not material:
            continue
        before = held.get(material)
        unit = (r.base_unit_of_measure or "").strip() or None
        held[material] = _Material(
            deleted=_flagged(r.df_at_client_level) or (before is not None and before.deleted),
            unit=before.unit if before is not None and before.unit else unit,
        )
    return held

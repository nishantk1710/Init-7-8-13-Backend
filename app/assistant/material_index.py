"""The rows the assistant's material search reads, held in memory.

Why
---
:mod:`app.assistant.material_search` asked the database on every keystroke. On
Azure SQL the plant lookup -- ``n_marc`` / ``n_mard`` with ``material IN (...)``
or ``material LIKE '8000%'`` -- runs against views that normalise the material
number, so no index applies and each lookup scans. Measured on non-prod (16 Oct
2026): a name with many candidates ("pump", "seal") or a short number prefix
("80000") took 21-22 s every time; a query matching nothing took 1.6 s. It is
the cause :mod:`app.shared.material_names` met and fixed the same way.

What is held
------------
Exactly the four sources the search reads, already restricted the way its
queries restrict them, so searching here gives the same answer:

* MAKT (``n_makt``) -- **every** text per material with its language, not only
  the English one. The search matches words against each text and prefers
  English among the texts that match; keeping one text per material would
  change which materials a non-English word finds.
* ZMM065 (``criticality_current``) -- material, plant, description at in-scope
  plants.
* MARC (``n_marc``) -- material, plant, MRP type at in-scope plants.
* MARD (``n_mard``) -- material, plant at in-scope plants.

Material numbers are normalised once, at load. A few tens of thousands of short
strings: a few MB.

Loaded whole, in the background, never per request -- the same arrangement as
:mod:`app.shared.material_names`. Until the first load lands (the minutes after
a deploy) :func:`current` returns ``None`` and the search keeps asking the
database, slow but correct. Reloaded after :data:`CACHE_SECONDS`; a load that
could not read a source is retried after :data:`RETRY_SECONDS`. A material new
to the extract reaches the search on the next reload. Opening a session reads
MARC itself, so routing is never this stale -- only the suggestions are.
"""

from __future__ import annotations

import bisect
import threading
import time
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i8.material_number import normalise
from app.shared.plant_scope import sql_predicate

logger = get_logger(__name__)

#: How long a loaded index is trusted before it is read again.
CACHE_SECONDS = 6 * 60 * 60

#: How soon a load that could not read every source is tried again.
RETRY_SECONDS = 10 * 60

#: How long start-up waits for the first load before the snapshot builds, so
#: they do not scan alongside it on the one-vCore database.
STARTUP_WAIT_SECONDS = 120

_REPORT_TABLE = "criticality_current"


@dataclass(frozen=True)
class MaktText:
    english: bool
    text: str
    upper: str


@dataclass(frozen=True)
class ReportRow:
    material: str
    plant: str
    description: str | None
    upper: str


@dataclass
class MaterialIndex:
    """Everything the search reads, keyed by normalised material number."""

    makt: dict[str, list[MaktText]] = field(default_factory=dict)
    report: list[ReportRow] = field(default_factory=list)
    marc: dict[str, dict[str, str]] = field(default_factory=dict)
    """``{material: {plant: MRP type}}``; a blank DISMM stays ``""``."""
    mard: dict[str, set[str]] = field(default_factory=dict)
    held: list[str] = field(default_factory=list)
    """Sorted materials held at an in-scope plant (MARC or MARD), for prefixes."""

    def finish(self) -> MaterialIndex:
        self.held = sorted(set(self.marc) | set(self.mard))
        return self

    @property
    def empty(self) -> bool:
        return not (self.makt or self.report or self.marc or self.mard)

    def with_prefix(self, prefix: str, limit: int) -> list[str]:
        """Held materials starting with ``prefix``, lowest first, at most ``limit``."""
        start = bisect.bisect_left(self.held, prefix)
        found: list[str] = []
        for material in self.held[start:]:
            if not material.startswith(prefix) or len(found) >= limit:
                break
            found.append(material)
        return found


def load_all(db: Session) -> tuple[MaterialIndex, bool]:
    """Read the four sources in full. Whether every one could be read."""
    index = MaterialIndex()
    complete = True

    def read(label: str, sql: str):  # noqa: ANN202 -- rows or None
        nonlocal complete
        try:
            return db.execute(text(sql))
        except DBAPIError as error:
            logger.info("material index: %s not readable (%s)", label, error.__class__.__name__)
            db.rollback()
            complete = False
            return None

    rows = read(
        "n_makt",
        "SELECT material, language_key, material_description FROM n_makt "
        "WHERE material <> '' AND material_description <> ''",
    )
    for row in rows or ():
        material = normalise(row.material)
        description = row.material_description or ""
        if not material:
            continue
        english = (row.language_key or "").strip().upper() in ("E", "EN")
        index.makt.setdefault(material, []).append(MaktText(english, description, description.upper()))

    rows = read(
        _REPORT_TABLE,
        f"SELECT material, plant, description FROM {_REPORT_TABLE} "
        f"WHERE material <> '' AND {sql_predicate('plant')}",
    )
    for row in rows or ():
        material = normalise(row.material)
        if not material:
            continue
        description = row.description
        index.report.append(
            ReportRow(material, (row.plant or "").strip(), description, (description or "").upper())
        )

    rows = read(
        "n_marc",
        f"SELECT material, plant, mrp_type FROM n_marc WHERE plant <> '' AND {sql_predicate('plant')}",
    )
    for row in rows or ():
        material = normalise(row.material)
        plant = (row.plant or "").strip()
        if material and plant:
            index.marc.setdefault(material, {})[plant] = (row.mrp_type or "").strip()

    rows = read(
        "n_mard",
        f"SELECT DISTINCT material, plant FROM n_mard WHERE plant <> '' AND {sql_predicate('plant')}",
    )
    for row in rows or ():
        material = normalise(row.material)
        plant = (row.plant or "").strip()
        if material and plant:
            index.mard.setdefault(material, set()).add(plant)

    return index.finish(), complete


# --- the loaded index ----------------------------------------------------------

_lock = threading.Lock()
_index: MaterialIndex | None = None
_next_load_at = 0.0
_loading: threading.Thread | None = None


def current() -> MaterialIndex | None:
    """The loaded index, or ``None`` while there is none yet.

    Never touches the database: starts a background load when there is no
    index yet or it has gone stale, and answers with what is held now.
    """
    _ensure_loading()
    with _lock:
        return _index


def start_loading() -> None:
    """Start a background load now if none is loaded or running. For start-up."""
    _ensure_loading()


def wait_for_load(timeout: float | None = None) -> None:
    """Block until a running background load ends. For start-up and tests."""
    with _lock:
        loading = _loading
    if loading is not None:
        loading.join(timeout)


def set_index(index: MaterialIndex | None) -> None:
    """For tests: hold this index (``None`` forgets it) and load nothing."""
    global _index, _next_load_at
    wait_for_load()
    with _lock:
        _index = index
        _next_load_at = float("inf") if index is not None else 0.0


def clear_cache() -> None:
    """For tests: forget the index, so the next call loads again."""
    set_index(None)


def _ensure_loading() -> None:
    global _loading
    with _lock:
        if time.monotonic() < _next_load_at or _loading is not None:
            return
        _loading = threading.Thread(target=_load, name="material-index", daemon=True)
        _loading.start()


def _load() -> None:
    global _index, _next_load_at, _loading
    # Imported here: the session factory reads settings, which tests and
    # scripts may set after this module is imported.
    from app.core.db import get_sessionmaker

    started = time.monotonic()
    retry_in: float = RETRY_SECONDS
    try:
        with get_sessionmaker()() as db:
            index, complete = load_all(db)
        with _lock:
            # Keep the previous index rather than replacing it with nothing.
            if not index.empty or _index is None:
                _index = None if index.empty else index
        retry_in = CACHE_SECONDS if complete else RETRY_SECONDS
        logger.info(
            "material index: %d named, %d held, %d report rows in %.1fs%s",
            len(index.makt), len(index.held), len(index.report),
            time.monotonic() - started, "" if complete else " (a source was unreadable)",
        )
    except Exception:  # noqa: BLE001 -- a failed load must never reach a request
        logger.exception("material index: load failed; search asks the database")
    finally:
        with _lock:
            _next_load_at = time.monotonic() + retry_in
            _loading = None

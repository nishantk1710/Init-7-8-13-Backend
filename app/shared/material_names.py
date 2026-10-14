"""Material names for screens that list material numbers.

The OAR screens listed bare numbers (``8000005632``), while I08's register
names every part. This answers "what is this material called?" from the same
sources the assistant's material search reads
(:mod:`app.assistant.material_search`), in the same order:

1. MAKT (``n_makt``), English text first;
2. the ZMM065 aging reports, which name roughly four times as many repair
   materials as MAKT does.

A material neither names is left out -- the screen shows the number alone
rather than an invented name.

Loaded whole, in the background, never per request
--------------------------------------------------
The first version looked names up per request with an IN list. On Azure SQL
``n_makt`` is a view that normalises the material number, so the IN list could
not use an index: a 1,000-row page took over two minutes and the screens timed
out. Both sources are small (a few tens of thousands of short strings), so they
are now read once, in full, on a background thread, and every request is a
dictionary lookup. A request never waits for the load: until it finishes,
:func:`material_names` returns nothing and the screens show numbers.

Two exceptions to "never waits", both bounded. Start-up loads the list right
after the normalise views (``app.main``), so it is usually ready before anyone
asks. And a caller may pass ``wait_seconds`` to :func:`attach_material_names`:
the assistant's session log does, because a page rendered in the gap after a
deploy otherwise showed numbers only, and search by name found nothing.

The list is reloaded after :data:`CACHE_SECONDS`; a load that could not read a
source is retried after :data:`RETRY_SECONDS`. A source that is not loaded
(``n_makt`` on a database whose normalise views were never built, a ZMM065
report never uploaded) is skipped, not an error.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i8.material_number import normalise

logger = get_logger(__name__)

#: How long a loaded list is trusted before it is read again.
CACHE_SECONDS = 6 * 60 * 60

#: How soon a load that could not read every source is tried again.
RETRY_SECONDS = 10 * 60

#: How long start-up waits for the first load before moving on to the
#: snapshot builds, so they do not run alongside it on the one-vCore database.
STARTUP_WAIT_SECONDS = 120

ZMM065_TABLES = ("raw_zmm065_bmm", "raw_zmm065_gb")

_lock = threading.Lock()
_names: dict[str, str] = {}
_next_load_at = 0.0
_loading: threading.Thread | None = None


def material_names(materials: Iterable[str | None]) -> dict[str, str]:
    """``{normalised material: name}`` for every material a source names.

    Never touches the database: it reads the loaded list, and starts a
    background load when there is none yet or it has gone stale.
    """
    _ensure_loading()
    with _lock:
        names = _names
    found: dict[str, str] = {}
    for material in materials:
        key = normalise(material)
        if key and key in names:
            found[key] = names[key]
    return found


def attach_material_names(
    rows: Sequence[Any], *, field: str = "material", wait_seconds: float = 0
) -> Sequence[Any]:
    """Set ``material_description`` on each response row from its ``field``.

    ``wait_seconds`` > 0: while no list has loaded yet and a load is running,
    wait up to that long for it rather than answering with numbers only.
    """
    if not rows:
        return rows
    if wait_seconds > 0:
        wait_for_first_load(wait_seconds)
    names = material_names(getattr(row, field) for row in rows)
    for row in rows:
        key = normalise(getattr(row, field))
        row.material_description = names.get(key) if key else None
    return rows


def load_all(db: Session) -> tuple[dict[str, str], bool]:
    """Every name both sources hold, and whether every source could be read."""
    names: dict[str, str] = {}
    complete = True

    try:
        english: set[str] = set()
        rows = db.execute(
            text(
                "SELECT material, language_key, material_description FROM n_makt "
                "WHERE material <> '' AND material_description <> ''"
            )
        )
        for row in rows:
            key = normalise(row.material)
            text_ = (row.material_description or "").strip()
            if not key or not text_ or key in english:
                continue
            is_english = (row.language_key or "").strip().upper() in ("E", "EN")
            if is_english or key not in names:
                names[key] = text_
            if is_english:
                english.add(key)
    except DBAPIError as error:
        logger.info("material names: n_makt not readable (%s)", error.__class__.__name__)
        db.rollback()
        complete = False

    for table in ZMM065_TABLES:
        try:
            rows = db.execute(
                text(
                    f"SELECT mat_code, material_description FROM {table} "
                    "WHERE mat_code <> '' AND material_description <> ''"
                )
            )
            for row in rows:
                key = normalise(row.mat_code)
                text_ = (row.material_description or "").strip()
                if key and text_ and key not in names:
                    names[key] = text_
        except DBAPIError as error:
            logger.info("material names: %s not readable (%s)", table, error.__class__.__name__)
            db.rollback()
            complete = False

    return names, complete


def wait_for_load(timeout: float | None = None) -> None:
    """Block until a running background load ends. For tests and scripts."""
    with _lock:
        loading = _loading
    if loading is not None:
        loading.join(timeout)


def start_loading() -> None:
    """Start a background load now if none is loaded or running. For start-up."""
    _ensure_loading()


def wait_for_first_load(timeout: float) -> bool:
    """Wait up to ``timeout`` seconds while nothing is loaded and a load runs.

    Returns at once when a list is already loaded, or when no load is running
    (one that failed is retried later, not waited for). True if names are
    available afterwards.
    """
    _ensure_loading()
    with _lock:
        if _names:
            return True
        loading = _loading
    if loading is not None:
        loading.join(timeout)
    with _lock:
        return bool(_names)


def clear_cache() -> None:
    """For tests: forget the list, so the next call loads again."""
    global _names, _next_load_at
    wait_for_load()
    with _lock:
        _names = {}
        _next_load_at = 0.0


def _ensure_loading() -> None:
    global _loading
    with _lock:
        if time.monotonic() < _next_load_at or _loading is not None:
            return
        _loading = threading.Thread(target=_load, name="material-names", daemon=True)
        _loading.start()


def _load() -> None:
    global _names, _next_load_at, _loading
    # Imported here: the session factory reads settings, which tests and
    # scripts may set after this module is imported.
    from app.core.db import get_sessionmaker

    started = time.monotonic()
    retry_in: float = RETRY_SECONDS
    try:
        with get_sessionmaker()() as db:
            names, complete = load_all(db)
        with _lock:
            # Keep the previous list rather than replacing it with nothing.
            if names or not _names:
                _names = names
        retry_in = CACHE_SECONDS if complete else RETRY_SECONDS
        logger.info(
            "material names: %d loaded in %.1fs%s",
            len(names), time.monotonic() - started, "" if complete else " (a source was unreadable)",
        )
    except Exception:  # noqa: BLE001 -- a failed load must never reach a request
        logger.exception("material names: load failed; screens show numbers only")
    finally:
        with _lock:
            _next_load_at = time.monotonic() + retry_in
            _loading = None

"""Material names for screens that list material numbers.

The OAR screens listed bare numbers (``8000005632``), while I08's register
names every part. This answers "what is this material called?" for a batch of
numbers, from the same sources the assistant's material search reads
(:mod:`app.assistant.material_search`), in the same order:

1. MAKT (``n_makt``), English text first;
2. the ZMM065 aging reports, which name roughly four times as many repair
   materials as MAKT does.

A material neither names is left out of the result -- the screen shows the
number alone rather than an invented name.

Read-only. Names change rarely and every OAR list asks for the same few
thousand, so found names (and misses) are kept in-process for
:data:`CACHE_SECONDS`. A source that is not loaded (``n_makt`` on a database
whose normalise views were never built, a ZMM065 report never uploaded) is
skipped, not an error, and is not cached as a miss.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i8.material_number import normalise
from app.shared.sql_lists import fetch_in_chunks

logger = get_logger(__name__)

#: How long a looked-up name (or the absence of one) is trusted.
CACHE_SECONDS = 6 * 60 * 60

ZMM065_TABLES = ("raw_zmm065_bmm", "raw_zmm065_gb")

_lock = threading.Lock()
_cache: dict[str, tuple[float, str | None]] = {}


def material_names(db: Session, materials: Iterable[str | None]) -> dict[str, str]:
    """``{normalised material: name}`` for every material a source names."""
    keys = {key for key in (normalise(m) for m in materials) if key}
    if not keys:
        return {}

    now = time.monotonic()
    names: dict[str, str] = {}
    missing: list[str] = []
    with _lock:
        for key in keys:
            hit = _cache.get(key)
            if hit is not None and now - hit[0] < CACHE_SECONDS:
                if hit[1]:
                    names[key] = hit[1]
            else:
                missing.append(key)

    if missing:
        found, complete = _lookup(db, sorted(missing))
        names.update(found)
        with _lock:
            for key in missing:
                # Only remember a miss when every source answered: a source
                # that was unreadable may simply not be loaded yet.
                if key in found or complete:
                    _cache[key] = (now, found.get(key))
    return names


def attach_material_names(db: Session, rows: Sequence[Any], *, field: str = "material") -> Sequence[Any]:
    """Set ``material_description`` on each response row from its ``field``."""
    if not rows:
        return rows
    names = material_names(db, (getattr(row, field) for row in rows))
    for row in rows:
        key = normalise(getattr(row, field))
        row.material_description = names.get(key) if key else None
    return rows


def clear_cache() -> None:
    """For tests."""
    with _lock:
        _cache.clear()


def _lookup(db: Session, keys: list[str]) -> tuple[dict[str, str], bool]:
    """Names for ``keys``, and whether every source could be read."""
    names: dict[str, str] = {}
    complete = True

    try:
        english: set[str] = set()
        rows = fetch_in_chunks(
            db,
            "SELECT material, language_key, material_description FROM n_makt "
            "WHERE material IN :materials AND material_description <> ''",
            "materials",
            keys,
        )
        for row in rows:
            key = normalise(row["material"])
            text_ = (row["material_description"] or "").strip()
            if not key or not text_ or key in english:
                continue
            is_english = (row["language_key"] or "").strip().upper() in ("E", "EN")
            if is_english or key not in names:
                names[key] = text_
            if is_english:
                english.add(key)
    except DBAPIError as error:
        logger.info("material names: n_makt not readable (%s)", error.__class__.__name__)
        db.rollback()
        complete = False

    remaining = [key for key in keys if key not in names]
    for table in ZMM065_TABLES:
        if not remaining:
            break
        # ZMM065 may carry the padded number, so it is matched after
        # normalising rather than in SQL.
        try:
            rows = fetch_in_chunks(
                db,
                f"SELECT mat_code, material_description FROM {table} "
                "WHERE mat_code IN :codes AND material_description <> ''",
                "codes",
                _with_padding(remaining),
            )
        except DBAPIError as error:
            logger.info("material names: %s not readable (%s)", table, error.__class__.__name__)
            db.rollback()
            complete = False
            continue
        for row in rows:
            key = normalise(row["mat_code"])
            text_ = (row["material_description"] or "").strip()
            if key and text_ and key not in names:
                names[key] = text_
        remaining = [key for key in remaining if key not in names]

    return names, complete


def _with_padding(keys: list[str]) -> list[str]:
    """Each key as stored unpadded and as SAP's 18-character MATNR."""
    return list(dict.fromkeys([*keys, *(key.zfill(18) for key in keys if key.isdigit())]))

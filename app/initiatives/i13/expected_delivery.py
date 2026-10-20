"""Expected delivery date (EKET-EINDT) for a PO line, for the utilisation ledger.

UAT, 21 Oct 2026: a ledger line not yet (fully) delivered shows when the rest
is expected -- the delivery date on the PO line's schedule line. "Delivered
on" stays the goods-receipt date (EKBE-BUDAT); this is the other half.

Which schedule line
-------------------
A PO line can have several schedule lines (EKET), each with its own date
(EINDT), scheduled quantity (MENGE) and delivered quantity (WEMNG). The
expected date is the earliest EINDT among the lines not yet fully delivered
(WEMNG < MENGE) -- the next delivery still to come. When every line is
delivered, it is the last EINDT. A PO line with no EKET row has none.

Why it is not in the stored snapshot
------------------------------------
Every field of a stored I13 type is part of the snapshot's schema signature
(``snapshot_store.codec.schema_signature``): adding one makes the served
version unreadable until a full rebuild (80-90 min of "building" screens).
EKET is small -- a few thousand schedule lines -- so it is read whole, in the
background, like ``app.shared.material_names``, and attached to the ledger
responses at the route. A request never waits for the load; until it lands
the field is null and the screen shows the status alone.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.logging import get_logger

logger = get_logger(__name__)

#: EKET changes with the hourly delta; half an hour is fresh enough for a
#: date that moves by days.
CACHE_SECONDS = 30 * 60

#: How soon a load that failed is tried again.
RETRY_SECONDS = 10 * 60

_QUERY = """
    SELECT purchasing_document, item, delivery_date, scheduled_quantity, qty_delivered
    FROM n_eket
    WHERE purchasing_document <> '' AND item <> ''
"""

Key = tuple[str, str]


def _as_date(raw: Any) -> date | None:
    """A view date as text (CSV views) or as a date (a typed column)."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError:
        return None


def _as_decimal(raw: Any) -> Decimal:
    if raw is None or raw == "":
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return Decimal("0")

_lock = threading.Lock()
_dates: dict[Key, date] = {}
_next_load_at = 0.0
_loading: threading.Thread | None = None


def next_expected(lines: Iterable[tuple[date | None, Decimal, Decimal]]) -> date | None:
    """``(EINDT, MENGE, WEMNG)`` per schedule line -> the expected date.

    The earliest date among lines not fully delivered; else the last date.
    Lines without a date are ignored.
    """
    dated = [(eindt, menge, wemng) for eindt, menge, wemng in lines if eindt is not None]
    if not dated:
        return None
    still_open = [eindt for eindt, menge, wemng in dated if wemng < menge]
    return min(still_open) if still_open else max(eindt for eindt, _, _ in dated)


def load_all(db: Session) -> tuple[dict[Key, date], bool]:
    """``{(PO, item): expected date}`` for every PO line EKET schedules."""
    lines: dict[Key, list[tuple[date | None, Decimal, Decimal]]] = defaultdict(list)
    try:
        for row in db.execute(text(_QUERY)):
            key = ((row.purchasing_document or "").strip(), (row.item or "").strip())
            if key[0] and key[1]:
                lines[key].append(
                    (_as_date(row.delivery_date), _as_decimal(row.scheduled_quantity), _as_decimal(row.qty_delivered))
                )
    except DBAPIError as error:
        logger.info("expected delivery: n_eket not readable (%s)", error.__class__.__name__)
        db.rollback()
        return {}, False
    found = {key: when for key, schedule in lines.items() if (when := next_expected(schedule)) is not None}
    return found, True


def expected_dates(keys: Iterable[tuple[str | None, str | None]]) -> dict[Key, date]:
    """The expected date for each (PO, item) that has one. Never touches the
    database: reads the loaded list, starting a background load when stale."""
    _ensure_loading()
    with _lock:
        dates = _dates
    found: dict[Key, date] = {}
    for po, item in keys:
        key = ((po or "").strip(), (item or "").strip())
        if key in dates:
            found[key] = dates[key]
    return found


def attach_expected_delivery(
    rows: Sequence[Any], *, po_field: str = "po_number", item_field: str = "po_item"
) -> Sequence[Any]:
    """Set ``expected_delivery_date`` on each response row from its PO line."""
    if not rows:
        return rows
    dates = expected_dates((getattr(r, po_field), getattr(r, item_field)) for r in rows)
    for row in rows:
        key = ((getattr(row, po_field) or "").strip(), (getattr(row, item_field) or "").strip())
        row.expected_delivery_date = dates.get(key)
    return rows


def start_loading() -> None:
    """Start a background load now if none is loaded or running. For start-up."""
    _ensure_loading()


def wait_for_load(timeout: float | None = None) -> None:
    """Block until a running background load ends. For tests and scripts."""
    with _lock:
        loading = _loading
    if loading is not None:
        loading.join(timeout)


def clear_cache() -> None:
    """For tests: forget the list, so the next call loads again."""
    global _dates, _next_load_at
    wait_for_load()
    with _lock:
        _dates = {}
        _next_load_at = 0.0


def _ensure_loading() -> None:
    global _loading
    with _lock:
        if time.monotonic() < _next_load_at or _loading is not None:
            return
        _loading = threading.Thread(target=_load, name="expected-delivery", daemon=True)
        _loading.start()


def _load() -> None:
    global _dates, _next_load_at, _loading
    from app.core.db import get_sessionmaker

    started = time.monotonic()
    retry_in: float = RETRY_SECONDS
    try:
        with get_sessionmaker()() as db:
            dates, complete = load_all(db)
        with _lock:
            # Keep the previous list rather than replacing it with nothing.
            if dates or not _dates:
                _dates = dates
        retry_in = CACHE_SECONDS if complete else RETRY_SECONDS
        logger.info("expected delivery: %d PO lines loaded in %.1fs", len(dates), time.monotonic() - started)
    except Exception:  # noqa: BLE001 -- a failed load must never reach a request
        logger.exception("expected delivery: load failed; the ledger shows no expected dates")
    finally:
        with _lock:
            _next_load_at = time.monotonic() + retry_in
            _loading = None

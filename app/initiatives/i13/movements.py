"""Movement normalisation: reversal-aware netting over goods-movement rows.

A single reusable layer, rather than embedding movement-type conditionals
inside every dashboard/aging/ledger query. Rows are plain normalized dicts
(``Bwart``, ``Menge``, ``BudatMkpf``, ...) as produced by
``app.integrations.sap.postgres_movements`` and consumed throughout
``app.initiatives.i13``.
"""

from datetime import date
from decimal import Decimal
from typing import Any

Row = dict[str, Any]

# Reversal movement type -> the movement type it reverses.
REVERSAL_OF: dict[str, str] = {"102": "101", "202": "201", "262": "261"}

RECEIPT_TYPES: frozenset[str] = frozenset({"101"})
ISSUE_TYPES: frozenset[str] = frozenset({"201", "261"})

_REVERSAL_TYPE_OF_BASE: dict[str, str] = {base: reversal for reversal, base in REVERSAL_OF.items()}


def is_reversal(movement_type: str) -> bool:
    """True if ``movement_type`` reverses an earlier movement (e.g. 102, 202, 262)."""
    return movement_type in REVERSAL_OF


def reversal_types_for(base_types: frozenset[str]) -> set[str]:
    """The reversal movement types that cancel any of ``base_types``."""
    return {_REVERSAL_TYPE_OF_BASE[bt] for bt in base_types if bt in _REVERSAL_TYPE_OF_BASE}


def net_quantity(rows: list[Row], base_types: frozenset[str]) -> Decimal:
    """Net quantity for ``base_types``, with any matching reversal subtracted.

    A reversed transaction never counts toward net consumption/receipt: a
    101 GR of qty 10 followed by a 102 reversal of qty 10 nets to zero.
    """
    reversal_types = reversal_types_for(base_types)
    total = Decimal("0")
    for row in rows:
        bwart = row.get("Bwart")
        qty = row.get("Menge") or Decimal("0")
        if bwart in base_types:
            total += qty
        elif bwart in reversal_types:
            total -= qty
    return total


def net_event_count(rows: list[Row], base_types: frozenset[str]) -> int:
    """Count of net movement events for ``base_types`` (base rows minus
    reversal rows), floored at zero -- a reversed event is not a genuine
    consumption/receipt event."""
    reversal_types = reversal_types_for(base_types)
    base_count = sum(1 for row in rows if row.get("Bwart") in base_types)
    reversal_count = sum(1 for row in rows if row.get("Bwart") in reversal_types)
    return max(base_count - reversal_count, 0)


def movement_date(row: Row) -> date | None:
    return row.get("BudatMkpf")


def filter_by_window(rows: list[Row], *, start: date | None, end: date | None) -> list[Row]:
    """Rows whose movement date falls within ``[start, end]`` (inclusive).
    Rows with no date are excluded rather than guessed into the window."""
    result = []
    for row in rows:
        moved_on = movement_date(row)
        if moved_on is None:
            continue
        if start is not None and moved_on < start:
            continue
        if end is not None and moved_on > end:
            continue
        result.append(row)
    return result


def latest_movement_date(rows: list[Row]) -> date | None:
    dates = [movement_date(row) for row in rows]
    dates = [d for d in dates if d is not None]
    return max(dates) if dates else None


def last_unreversed_date(
    rows: list[Row], base_types: frozenset[str], *, as_of: date | None = None
) -> date | None:
    """Latest date of a ``base_types`` event that a reversal has not cancelled.

    ``latest_movement_date`` over issue rows treats a 261 that was immediately
    reversed by a 262 as a real issue, which makes a material that was never
    actually consumed read as recently moving. Here each reversal cancels the
    most recent still-standing event it can reverse (a 262 cancels a 261, a
    202 a 201), in posting-date order -- SAP posts the reversal after the
    document it reverses, and the extract carries no reversal-document
    reference to pair them more precisely.

    ``as_of`` ignores everything posted after it, so the answer is the one that
    was true on that day (a reversal posted later did not yet exist).
    """
    reversal_base = {rev: base for rev, base in REVERSAL_OF.items() if base in base_types}
    events = sorted(
        (moved_on, row.get("Bwart"))
        for row in rows
        if (row.get("Bwart") in base_types or row.get("Bwart") in reversal_base)
        and (moved_on := movement_date(row)) is not None
        and (as_of is None or moved_on <= as_of)
    )
    standing: dict[str, list[date]] = {base: [] for base in base_types}
    for moved_on, bwart in events:
        if bwart in standing:
            standing[bwart].append(moved_on)
        elif standing[reversal_base[bwart]]:
            standing[reversal_base[bwart]].pop()
    latest = [dates[-1] for dates in standing.values() if dates]
    return max(latest) if latest else None


def event_dates(rows: list[Row], base_types: frozenset[str]) -> tuple[date | None, date | None]:
    """First/latest date among ``rows`` whose ``Bwart`` is a base type
    (excludes reversal rows, which don't represent a genuine event date).

    Shared by ``ledger.py`` (CSV-backed GR/GI dates) and
    ``procurement_chain.py`` (Postgres-backed GR dates) -- one definition of
    "the date of an event", not two.
    """
    dates = sorted(d for row in rows if row.get("Bwart") in base_types and (d := movement_date(row)) is not None)
    if not dates:
        return None, None
    return dates[0], dates[-1]

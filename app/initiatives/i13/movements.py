"""Movement normalisation: reversal-aware netting over goods-movement rows.

A single reusable layer, rather than embedding movement-type conditionals
inside every dashboard/aging/ledger query. Rows are plain
``GoodsMovementItemSet`` dicts (``Bwart``, ``Menge``, ``BudatMkpf``, ...) as
returned by ``SapGateway.get_goods_movements()``.
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

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


def net_standing_quantity(rows: list[Row], base_types: frozenset[str]) -> Decimal:
    """``net_quantity``, but a reversal only takes back what was posted before
    it, in posting-date order -- so it never goes below zero.

    A reservation's issues can hold a 262 whose 261 predates the movement
    history; plain netting would read that as a negative issue.

    On one posting date the base movements go first: SAP posts a reversal
    after the document it reverses, and a 262 handed over ahead of its
    same-day 261 would take back nothing and leave the 261 counted
    (reservation 1975548/7: 261 1, 262 1, 261 1 read as 2 issued, not 1).
    """
    reversal_types = reversal_types_for(base_types)
    total = Decimal("0")
    ordered = sorted(
        rows,
        key=lambda r: (movement_date(r) is None, movement_date(r) or date.min, r.get("Bwart") in reversal_types),
    )
    for row in ordered:
        bwart = row.get("Bwart")
        qty = row.get("Menge") or Decimal("0")
        if bwart in base_types:
            total += qty
        elif bwart in reversal_types:
            total = max(total - qty, Decimal("0"))
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


def standing_events(
    rows: list[Row], base_types: frozenset[str], *, as_of: date | None = None
) -> list[tuple[Row, Decimal]]:
    """Each dated ``base_types`` movement with the quantity its reversals left
    standing -- the one pairing of reversals to movements that the last issue
    date, the 12-month figures and the monthly usage all share.

    A reversal is booked against the movement it cancels, not against the
    month it was posted in: a 261 on 10 Jan reversed by a 262 on 5 Mar leaves
    January at 0, and March is not -1. SAP posts the reversal after the
    document it reverses, and the extract carries no reversal-document
    reference (MSEG-SMBLN), so each reversal takes its quantity back from the
    most recent movement still standing that it can reverse (a 262 from a 261,
    a 202 from a 201), in posting-date order, a movement before a same-day
    reversal. A reversal with nothing standing to take back from reverses a
    movement older than the history held, and is ignored.

    ``as_of`` ignores everything posted after it, so the answer is the one
    that was true on that day (a reversal posted later did not yet exist).
    Movements are returned in posting order; a fully reversed one has 0.
    """
    reversal_base = {rev: base for rev, base in REVERSAL_OF.items() if base in base_types}
    dated = sorted(
        (
            row
            for row in rows
            if (row.get("Bwart") in base_types or row.get("Bwart") in reversal_base)
            and (moved_on := movement_date(row)) is not None
            and (as_of is None or moved_on <= as_of)
        ),
        key=lambda row: (movement_date(row), row.get("Bwart") in reversal_base),
    )
    events: list[list] = []
    stacks: dict[str, list[list]] = {base: [] for base in base_types}
    for row in dated:
        bwart = row.get("Bwart")
        qty = row.get("Menge") or Decimal(0)
        if bwart in base_types:
            event = [row, qty]
            events.append(event)
            stacks[bwart].append(event)
            continue
        stack = stacks[reversal_base[bwart]]
        while qty > 0 and stack:
            top = stack[-1]
            take = min(qty, top[1])
            top[1] -= take
            qty -= take
            if top[1] <= 0:
                stack.pop()
    return [(row, standing) for row, standing in events]


def last_unreversed_date(
    rows: list[Row], base_types: frozenset[str], *, as_of: date | None = None
) -> date | None:
    """Latest date of a ``base_types`` movement a reversal has not fully cancelled.

    ``latest_movement_date`` over issue rows treats a 261 that was immediately
    reversed by a 262 as a real issue, which makes a material that was never
    actually consumed read as recently moving. Paired as :func:`standing_events`
    pairs them, so the Usage tab's last issue month is this date's month.
    """
    dates = [movement_date(row) for row, standing in standing_events(rows, base_types, as_of=as_of) if standing > 0]
    return max(dates) if dates else None


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

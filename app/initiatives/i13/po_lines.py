"""Whether anything can still arrive on a PO line -- one rule for every I13 screen.

UAT, 23 Oct 2026: the assistant's open-PO quantity and the utilisation
ledger's "Not delivered" disagreed (5000046515 at 1300: 2 against 6, and the
truth was 0), because each decided "open" its own way. Measured over the OAR
population, 14,632 PO lines read as not delivered and 175 of them were.

A PO line has nothing more to come when SAP says so:

* ``NOT_AN_ORDER`` -- its document is not an order (EKKO-BSART in
  ``Settings.i13_non_order_doc_types``: contracts and requests for quotation),
  or it orders nothing (EKPO-MENGE 0: a contract line's quantity is its target,
  KTMNG, which an order never receives against).
* ``CLOSED`` -- EKPO-LOEKZ is set (``L`` deleted, ``S`` blocked) or EKPO-ELIKZ
  is ``X`` (delivery completed: nothing more will be delivered, even on a line
  closed short).

Every other line is a real order still open for whatever it has not received.
Switch: ``Settings.i13_open_po_respect_closed`` (off, every line is an order
and none is closed -- the behaviour before 21 Oct).
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Any, Protocol

from app.core.config import get_settings

Row = dict[str, Any]
Key = tuple[str, str]


class PoLineState(str, Enum):
    NOT_AN_ORDER = "NOT_AN_ORDER"
    CLOSED = "CLOSED"


class _PoItemSource(Protocol):
    def get_purchase_order_items(
        self, *, po_number: str | None = None, pr_number: str | None = None,
        material: str | None = None, plant: str | None = None,
    ) -> list[Row]: ...


def po_line_state(row: Row) -> PoLineState | None:
    """Why nothing more can arrive on this EKPO row, or None when it is open."""
    settings = get_settings()
    if not settings.i13_open_po_respect_closed:
        return None
    # An unknown quantity (None) is not a zero one: it says nothing either way.
    if (row.get("Bsart") or "") in settings.i13_non_order_doc_type_set or row.get("Menge") == Decimal("0"):
        return PoLineState.NOT_AN_ORDER
    if row.get("Loekz") or row.get("Elikz") == "X":
        return PoLineState.CLOSED
    return None


def po_line_states(
    repository: _PoItemSource, *, material: str | None = None, plant: str | None = None
) -> dict[Key, PoLineState]:
    """``po_line_state`` for every PO line that is not open, by (EBELN, EBELP).

    Read with the same arguments ``build_procurement_chain`` uses, so the
    repository's per-instance cache answers it without a second query.
    """
    states: dict[Key, PoLineState] = {}
    for row in repository.get_purchase_order_items(pr_number=None, material=material, plant=plant):
        state = po_line_state(row)
        if state is not None:
            states[(row["Ebeln"], row["Ebelp"])] = state
    return states

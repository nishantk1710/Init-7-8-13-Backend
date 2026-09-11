"""Deterministic consumption attribution.

Attribution is decided purely from the references already resolved on a
ledger entry -- no scoring, no probabilistic matching, no Initiative 10
logic. Precedence: an exact reservation-item match beats a reservation
matched only at the order/header level (``Rsnum`` without ``Rspos``), which
beats a bare procurement (PO-only, not yet issued) link.
"""

from app.initiatives.i13.models import AttributionResult, AttributionStatus, UtilisationLedgerEntry


def attribute_consumption(entry: UtilisationLedgerEntry) -> AttributionResult:
    if entry.issued_quantity > 0 and entry.reservation_item is not None:
        return AttributionResult(
            ledger_id=entry.ledger_id,
            status=AttributionStatus.RESERVATION_LINK,
            evidence=f"Rsnum {entry.reservation_number}/{entry.reservation_item} matched to goods issue",
        )
    if entry.issued_quantity > 0 and entry.reservation_number is not None:
        return AttributionResult(
            ledger_id=entry.ledger_id,
            status=AttributionStatus.ORDER_LINK,
            evidence=f"Rsnum {entry.reservation_number} matched to goods issue without an exact reservation item",
        )
    if entry.received_quantity > 0:
        return AttributionResult(
            ledger_id=entry.ledger_id,
            status=AttributionStatus.PROCUREMENT_LINK,
            evidence=f"PO {entry.po_number}/{entry.po_item} received, not yet issued",
        )
    return AttributionResult(
        ledger_id=entry.ledger_id,
        status=AttributionStatus.UNATTRIBUTED,
        evidence="No reservation, order, or procurement reference resolved evidence of consumption",
    )

"""Deterministic consumption attribution.

Attribution is decided purely from the references already resolved on a
ledger entry -- no scoring, no probabilistic matching, no Initiative 10
logic.

Ported onto ``ReservationLedgerEntry`` (W6.2): unlike the old CSV-backed
``UtilisationLedgerEntry``, goods-issue attribution here is always resolved
via the reservation's own exact (RSNUM, RSPOS) key when any issue exists --
there is no "order matched without an item" intermediate case anymore, since
``reservation_ledger.py`` never looks up GI any other way. ``RESERVATION_LINK``
therefore covers every case with issued quantity; ``ORDER_LINK`` is unused
under this model and kept only so ``AttributionStatus`` doesn't need to
change shape for existing consumers.
"""

from app.initiatives.i13.models import AttributionResult, AttributionStatus, ReservationLedgerEntry


def attribute_consumption(entry: ReservationLedgerEntry) -> AttributionResult:
    if entry.issued_quantity > 0:
        return AttributionResult(
            ledger_id=entry.ledger_id,
            status=AttributionStatus.RESERVATION_LINK,
            evidence=f"Rsnum {entry.reservation_number}/{entry.reservation_item} matched to goods issue",
        )
    if entry.received_quantity and entry.received_quantity > 0:
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

"""Live PR -> PO -> GR -> GI utilisation ledger, plus the reservation leg.

Joins are deterministic SAP document references only (``Banfn``/``Bnfpo``,
``Ebeln``/``Ebelp``, ``Rsnum``/``Rspos``) -- no fuzzy matching, no LLM
stitching, no Initiative 10 logic. The reservation leg is built the same way
production will eventually see it, even though ``ReservationItemSet`` is
sourced from the reduced mock gateway today.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.integrations.sap.gateway import SapGateway
from app.initiatives.i13.models import LedgerUtilisationStatus, LinkageStatus, ProcurementStatus, UtilisationLedgerEntry
from app.initiatives.i13.movements import ISSUE_TYPES, RECEIPT_TYPES, event_dates, net_quantity

Row = dict[str, Any]


@dataclass
class _Indexes:
    reservations_by_pr: dict[tuple[str, str], Row]
    po_items_by_pr: dict[tuple[str, str], Row]
    movements_by_po: dict[tuple[str, str], list[Row]]
    movements_by_rsnum: dict[str, list[Row]]


def _build_indexes(
    reservations: list[Row], po_items: list[Row], movements: list[Row]
) -> _Indexes:
    reservations_by_pr: dict[tuple[str, str], Row] = {}
    for res in reservations:
        banfn, bnfpo = res.get("Banfn"), res.get("Bnfpo")
        if banfn and bnfpo:
            reservations_by_pr[(banfn, bnfpo)] = res

    po_items_by_pr: dict[tuple[str, str], Row] = {}
    for po_item in po_items:
        banfn, bnfpo = po_item.get("Banfn"), po_item.get("Bnfpo")
        if banfn and bnfpo:
            po_items_by_pr[(banfn, bnfpo)] = po_item

    movements_by_po: dict[tuple[str, str], list[Row]] = {}
    movements_by_rsnum: dict[str, list[Row]] = {}
    for movement in movements:
        ebeln, ebelp = movement.get("Ebeln"), movement.get("Ebelp")
        if ebeln and ebelp:
            movements_by_po.setdefault((ebeln, ebelp), []).append(movement)
        rsnum = movement.get("Rsnum")
        if rsnum:
            movements_by_rsnum.setdefault(rsnum, []).append(movement)

    return _Indexes(reservations_by_pr, po_items_by_pr, movements_by_po, movements_by_rsnum)


def _procurement_status(received_qty: Decimal, ordered_qty: Decimal | None) -> ProcurementStatus:
    if received_qty <= 0:
        return ProcurementStatus.OPEN
    if ordered_qty and received_qty < ordered_qty:
        return ProcurementStatus.PARTIALLY_RECEIVED
    return ProcurementStatus.RECEIVED


def _utilisation_status(issued_qty: Decimal, received_qty: Decimal) -> LedgerUtilisationStatus:
    if issued_qty <= 0:
        return LedgerUtilisationStatus.NOT_ISSUED
    if issued_qty < received_qty:
        return LedgerUtilisationStatus.PARTIALLY_ISSUED
    return LedgerUtilisationStatus.FULLY_ISSUED


def build_ledger_entry(pr: Row, indexes: _Indexes) -> UtilisationLedgerEntry:
    banfn, bnfpo = pr.get("Banfn"), pr.get("Bnfpo")
    material, plant = pr.get("Matnr"), pr.get("Werks")

    reservation = indexes.reservations_by_pr.get((banfn, bnfpo))
    if reservation is not None:
        reservation_number = reservation.get("Rsnum")
        reservation_item = reservation.get("Rspos")
        reservation_source = "MOCK"
    else:
        reservation_number = pr.get("Rsnum") or None
        reservation_item = None
        reservation_source = "UNAVAILABLE" if not reservation_number else "LIVE"

    po_item = indexes.po_items_by_pr.get((banfn, bnfpo))
    if po_item is not None:
        po_number, po_item_number = po_item.get("Ebeln"), po_item.get("Ebelp")
        ordered_qty = po_item.get("Menge")
        po_source = "LIVE"
    else:
        po_number = pr.get("Ebeln") or None
        po_item_number = pr.get("Ebelp") or None
        ordered_qty = pr.get("Bsmng") or pr.get("Menge")
        po_source = "LIVE" if po_number else "UNAVAILABLE"

    gr_rows = indexes.movements_by_po.get((po_number, po_item_number), []) if po_number and po_item_number else []
    received_qty = net_quantity(gr_rows, RECEIPT_TYPES)
    first_gr_date, latest_gr_date = event_dates(gr_rows, RECEIPT_TYPES)

    if reservation_item is not None:
        gi_rows = [
            m
            for m in indexes.movements_by_rsnum.get(reservation_number, [])
            if m.get("Rspos") == reservation_item
        ]
    elif reservation_number:
        gi_rows = indexes.movements_by_rsnum.get(reservation_number, [])
    else:
        gi_rows = []
    issued_qty = net_quantity(gi_rows, ISSUE_TYPES)
    first_gi_date, latest_gi_date = event_dates(gi_rows, ISSUE_TYPES)

    if reservation is not None:
        linkage_status = LinkageStatus.RESERVATION_LINKED
    elif po_number and received_qty > 0:
        linkage_status = LinkageStatus.FULL_CHAIN
    elif po_number:
        linkage_status = LinkageStatus.PR_ONLY
    else:
        linkage_status = LinkageStatus.UNMATCHED

    data_source = (
        f"reservation:{reservation_source},pr:LIVE,po:{po_source},gr:LIVE,gi:LIVE"
    )

    return UtilisationLedgerEntry(
        ledger_id=f"LEDG-{banfn}-{bnfpo}",
        material=material,
        plant=plant,
        reservation_number=reservation_number,
        reservation_item=reservation_item,
        pr_number=banfn,
        pr_item=bnfpo,
        po_number=po_number,
        po_item=po_item_number,
        received_quantity=received_qty,
        issued_quantity=issued_qty,
        open_quantity=received_qty - issued_qty,
        first_gr_date=first_gr_date,
        latest_gr_date=latest_gr_date,
        first_gi_date=first_gi_date,
        latest_gi_date=latest_gi_date,
        procurement_status=_procurement_status(received_qty, ordered_qty),
        utilisation_status=_utilisation_status(issued_qty, received_qty),
        linkage_status=linkage_status,
        data_source=data_source,
    )


def build_utilisation_ledger(gateway: SapGateway) -> list[UtilisationLedgerEntry]:
    """Build the full I13 utilisation ledger from the SAP gateway's live +
    reduced-mock data. One entry per purchase-requisition item."""
    pr_result = gateway.get_purchase_requisitions()
    po_items_result = gateway.get_purchase_order_items()
    movements_result = gateway.get_goods_movements()
    reservations_result = gateway.get_reservations()

    indexes = _build_indexes(reservations_result.rows, po_items_result.rows, movements_result.rows)
    return [build_ledger_entry(pr, indexes) for pr in pr_result.rows]

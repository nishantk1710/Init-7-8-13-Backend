"""PR -> PO -> GR -> GI ledger joins: full chain, reservation leg, partial
quantities, and unmatched chains -- deterministic document references only.
"""

from pathlib import Path

from app.initiatives.i13.ledger import build_utilisation_ledger
from app.initiatives.i13.models import LedgerUtilisationStatus, LinkageStatus, ProcurementStatus
from app.integrations.sap.gateway import SapGateway
from tests.i13.conftest import write_csv

PR_HEADER = [
    "Banfn", "Bnfpo", "Matnr", "Werks", "Menge", "Bsmng", "Ebeln", "Ebelp", "Rsnum",
]
PO_ITEM_HEADER = ["Ebeln", "Ebelp", "Banfn", "Bnfpo", "Matnr", "Werks", "Menge"]
MOVEMENT_HEADER = ["Mblnr", "Bwart", "Matnr", "Werks", "Menge", "Ebeln", "Ebelp", "Rsnum", "Rspos", "BudatMkpf"]
RESERVATION_HEADER = ["Rsnum", "Rspos", "Banfn", "Bnfpo", "Matnr", "Werks"]


def _build_gateway(data_dir: Path, *, pr_rows, po_item_rows, movement_rows, reservation_rows=()) -> SapGateway:
    write_csv(data_dir / "sap" / "PurchaseRequisitionSet.csv", PR_HEADER, pr_rows)
    write_csv(data_dir / "sap" / "PurchaseOrderItemSet.csv", PO_ITEM_HEADER, po_item_rows)
    write_csv(data_dir / "sap" / "GoodsMovementItemSet.csv", MOVEMENT_HEADER, movement_rows)
    write_csv(data_dir / "sap" / "ReservationItemSet.csv", RESERVATION_HEADER, reservation_rows)
    return SapGateway(data_dir)


def test_full_chain_with_reservation_leg(data_dir: Path) -> None:
    gateway = _build_gateway(
        data_dir,
        pr_rows=[{"Banfn": "2000000000", "Bnfpo": "00010", "Matnr": "MAT1", "Werks": "1000"}],
        po_item_rows=[
            {"Ebeln": "4500000000", "Ebelp": "00010", "Banfn": "2000000000", "Bnfpo": "00010", "Menge": "10"}
        ],
        movement_rows=[
            {"Mblnr": "1", "Bwart": "101", "Ebeln": "4500000000", "Ebelp": "00010", "Menge": "10", "BudatMkpf": "2025-01-01"},
            {"Mblnr": "2", "Bwart": "261", "Rsnum": "1000000000", "Rspos": "0001", "Menge": "6", "BudatMkpf": "2025-02-01"},
        ],
        reservation_rows=[{"Rsnum": "1000000000", "Rspos": "0001", "Banfn": "2000000000", "Bnfpo": "00010"}],
    )
    entries = build_utilisation_ledger(gateway)
    assert len(entries) == 1
    entry = entries[0]

    assert entry.reservation_number == "1000000000"
    assert entry.reservation_item == "0001"
    assert entry.po_number == "4500000000"
    assert entry.received_quantity == 10
    assert entry.issued_quantity == 6
    assert entry.open_quantity == 4
    assert entry.procurement_status is ProcurementStatus.RECEIVED
    assert entry.utilisation_status is LedgerUtilisationStatus.PARTIALLY_ISSUED
    assert entry.linkage_status is LinkageStatus.RESERVATION_LINKED
    assert "reservation:MOCK" in entry.data_source


def test_partial_receipt_against_ordered_quantity(data_dir: Path) -> None:
    gateway = _build_gateway(
        data_dir,
        pr_rows=[{"Banfn": "2000000001", "Bnfpo": "00010", "Matnr": "MAT1", "Werks": "1000"}],
        po_item_rows=[
            {"Ebeln": "4500000001", "Ebelp": "00010", "Banfn": "2000000001", "Bnfpo": "00010", "Menge": "20"}
        ],
        movement_rows=[
            {"Mblnr": "1", "Bwart": "101", "Ebeln": "4500000001", "Ebelp": "00010", "Menge": "10", "BudatMkpf": "2025-01-01"},
        ],
    )
    entry = build_utilisation_ledger(gateway)[0]
    assert entry.received_quantity == 10
    assert entry.procurement_status is ProcurementStatus.PARTIALLY_RECEIVED
    assert entry.utilisation_status is LedgerUtilisationStatus.NOT_ISSUED


def test_unmatched_chain_with_no_po(data_dir: Path) -> None:
    gateway = _build_gateway(
        data_dir,
        pr_rows=[{"Banfn": "2000000002", "Bnfpo": "00010", "Matnr": "MAT1", "Werks": "1000"}],
        po_item_rows=[],
        movement_rows=[],
    )
    entry = build_utilisation_ledger(gateway)[0]
    assert entry.po_number is None
    assert entry.received_quantity == 0
    assert entry.procurement_status is ProcurementStatus.OPEN
    assert entry.linkage_status is LinkageStatus.UNMATCHED


def test_gr_reversal_is_not_counted_as_received(data_dir: Path) -> None:
    gateway = _build_gateway(
        data_dir,
        pr_rows=[{"Banfn": "2000000003", "Bnfpo": "00010", "Matnr": "MAT1", "Werks": "1000"}],
        po_item_rows=[
            {"Ebeln": "4500000003", "Ebelp": "00010", "Banfn": "2000000003", "Bnfpo": "00010", "Menge": "10"}
        ],
        movement_rows=[
            {"Mblnr": "1", "Bwart": "101", "Ebeln": "4500000003", "Ebelp": "00010", "Menge": "10", "BudatMkpf": "2025-01-01"},
            {"Mblnr": "2", "Bwart": "102", "Ebeln": "4500000003", "Ebelp": "00010", "Menge": "10", "BudatMkpf": "2025-01-05"},
        ],
    )
    entry = build_utilisation_ledger(gateway)[0]
    assert entry.received_quantity == 0
    assert entry.procurement_status is ProcurementStatus.OPEN

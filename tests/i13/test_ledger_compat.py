"""GET /api/i13/ledger compatibility shim -- reproduces the retired
CSV-backed contract's exact shape/grain from real Postgres logic.
"""

from decimal import Decimal

from app.initiatives.i13.ledger_compat import LinkageStatus, ProcurementStatus, build_legacy_ledger
from tests.i13.conftest import FakeProcurementRepository, FakeReservationRepository, oar_scope_index


def _pr(banfn, bnfpo, material, plant, qty):
    return {"Banfn": banfn, "Bnfpo": bnfpo, "Matnr": material, "Werks": plant, "Menge": Decimal(qty)}


def _po(ebeln, ebelp, material, plant, qty, banfn=None, bnfpo=None):
    return {"Ebeln": ebeln, "Ebelp": ebelp, "Matnr": material, "Werks": plant, "Menge": Decimal(qty), "Banfn": banfn, "Bnfpo": bnfpo}


def _gr(ebeln, ebelp, bwart, qty, day):
    from datetime import date

    return {"Ebeln": ebeln, "Ebelp": ebelp, "Bwart": bwart, "Menge": Decimal(qty), "BudatMkpf": date(2026, 1, 1 + day)}


def _reservation(rsnum, rspos, material, plant, banfn, bnfpo):
    return {"Rsnum": rsnum, "Rspos": rspos, "Matnr": material, "Werks": plant, "Banfn": banfn, "Bnfpo": bnfpo}


def _gi(rsnum, rspos, bwart, qty, day):
    from datetime import date

    return {"Rsnum": rsnum, "Rspos": rspos, "Bwart": bwart, "Menge": Decimal(qty), "BudatMkpf": date(2026, 2, day)}


def test_pr_with_matching_reservation_resolves_gi_and_is_reservation_linked() -> None:
    procurement_repo = FakeProcurementRepository(
        prs=[_pr("2000000001", "10", "MAT1", "1300", "5")],
        po_items=[_po("4500000001", "10", "MAT1", "1300", "5", banfn="2000000001", bnfpo="10")],
        gr_rows=[_gr("4500000001", "10", "101", "5", 1)],
    )
    reservation_repo = FakeReservationRepository(
        reservations=[_reservation("1000000000", "0001", "MAT1", "1300", "2000000001", "10")],
        gi_rows=[_gi("1000000000", "0001", "261", "3", 1)],
    )
    scope = oar_scope_index(("MAT1", "1300"))

    entries = build_legacy_ledger(procurement_repo, reservation_repo, scope, plant="1300")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.reservation_number == "1000000000"
    assert entry.reservation_item == "0001"
    assert entry.issued_quantity == Decimal("3")
    assert entry.open_quantity == Decimal("2")
    assert entry.linkage_status is LinkageStatus.RESERVATION_LINKED
    assert entry.procurement_status is ProcurementStatus.RECEIVED
    assert entry.ledger_id == "LEDG-2000000001-10"


def test_pr_with_no_reservation_stays_unmatched_with_zero_issued() -> None:
    procurement_repo = FakeProcurementRepository(prs=[_pr("2000000002", "10", "MAT1", "1300", "5")])
    reservation_repo = FakeReservationRepository()
    scope = oar_scope_index(("MAT1", "1300"))

    entries = build_legacy_ledger(procurement_repo, reservation_repo, scope, plant="1300")
    entry = entries[0]
    assert entry.reservation_number is None
    assert entry.issued_quantity == Decimal("0")
    assert entry.linkage_status is LinkageStatus.UNMATCHED


def test_po_without_reservation_is_full_chain_when_received() -> None:
    procurement_repo = FakeProcurementRepository(
        prs=[_pr("2000000003", "10", "MAT1", "1300", "5")],
        po_items=[_po("4500000003", "10", "MAT1", "1300", "5", banfn="2000000003", bnfpo="10")],
        gr_rows=[_gr("4500000003", "10", "101", "5", 1)],
    )
    reservation_repo = FakeReservationRepository()
    scope = oar_scope_index(("MAT1", "1300"))

    entries = build_legacy_ledger(procurement_repo, reservation_repo, scope, plant="1300")
    entry = entries[0]
    assert entry.linkage_status is LinkageStatus.FULL_CHAIN
    assert entry.reservation_number is None


def test_non_oar_material_excluded_by_default() -> None:
    procurement_repo = FakeProcurementRepository(prs=[_pr("2000000004", "10", "MAT-VB", "1300", "5")])
    reservation_repo = FakeReservationRepository()
    scope = {("MAT-VB", "1300"): "VB"}

    entries = build_legacy_ledger(procurement_repo, reservation_repo, scope, plant="1300")
    assert entries == []

    entries_unscoped = build_legacy_ledger(
        procurement_repo, reservation_repo, scope, plant="1300", include_out_of_scope=True
    )
    assert len(entries_unscoped) == 1

"""GET /api/i13/ledger compatibility shim -- reproduces the retired
CSV-backed contract's exact shape/grain from real Postgres logic.
"""

from decimal import Decimal

from app.initiatives.i13.ledger_compat import LinkageStatus, ProcurementStatus, build_legacy_ledger, delivery_state
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
    procurement_repo = FakeProcurementRepository(prs=[_pr("2000000004", "10", "MAT-POD", "1300", "5")])
    reservation_repo = FakeReservationRepository()
    scope = {("MAT-POD", "1300"): "POD"}

    entries = build_legacy_ledger(procurement_repo, reservation_repo, scope, plant="1300")
    assert entries == []

    entries_unscoped = build_legacy_ledger(
        procurement_repo, reservation_repo, scope, plant="1300", include_out_of_scope=True
    )
    assert len(entries_unscoped) == 1


def _po_line_ledger(po_items, gr_rows=()):
    procurement_repo = FakeProcurementRepository(po_items=po_items, gr_rows=list(gr_rows))
    entries = build_legacy_ledger(procurement_repo, FakeReservationRepository(), oar_scope_index(("MAT1", "1300")))
    return {e.po_number: e for e in entries}


def test_a_line_nothing_more_can_arrive_on_is_never_waiting_for_delivery() -> None:
    """UAT, 23 Oct: 5000046515 at 1300 read 6 "Not delivered" lines and had
    none -- five contract lines (BSART WK/MK, MENGE 0) and one line closed
    with ELIKZ. Each reads as what it is."""
    contract = {**_po("4600000926", "16100", "MAT1", "1300", "0"), "Bsart": "WK"}
    rfq = {**_po("6000000001", "10", "MAT1", "1300", "4"), "Bsart": "AN"}
    closed = {**_po("4100105958", "10", "MAT1", "1300", "2"), "Bsart": "ZDOM", "Elikz": "X"}
    deleted = {**_po("4100000001", "10", "MAT1", "1300", "3"), "Loekz": "L"}
    short = {**_po("4100000002", "10", "MAT1", "1300", "10"), "Elikz": "X"}
    done = {**_po("4100000003", "10", "MAT1", "1300", "5"), "Elikz": "X"}
    still_open = {**_po("4100000004", "10", "MAT1", "1300", "5"), "Bsart": "ZDOM"}
    entries = _po_line_ledger(
        [contract, rfq, closed, deleted, short, done, still_open],
        [_gr("4100000002", "10", "101", "6", 1), _gr("4100000003", "10", "101", "5", 1)],
    )

    status = {po: e.procurement_status for po, e in entries.items()}
    assert status == {
        "4600000926": ProcurementStatus.NOT_AN_ORDER,
        "6000000001": ProcurementStatus.NOT_AN_ORDER,
        "4100105958": ProcurementStatus.CLOSED,
        "4100000001": ProcurementStatus.CLOSED,
        "4100000002": ProcurementStatus.CLOSED,  # closed short: 6 of 10, no more coming
        "4100000003": ProcurementStatus.RECEIVED,  # received in full: simply delivered
        "4100000004": ProcurementStatus.OPEN,
    }
    delivery = {po: delivery_state(e) for po, e in entries.items()}
    assert [po for po, d in delivery.items() if d == "NOT_DELIVERED"] == ["4100000004"]
    assert delivery["4600000926"] == "NOT_AN_ORDER"
    assert delivery["4100105958"] == "CLOSED"


def test_with_the_switch_off_every_line_reads_from_its_quantities(monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "i13_open_po_respect_closed", False)
    contract = {**_po("4600000926", "16100", "MAT1", "1300", "0"), "Bsart": "WK"}
    closed = {**_po("4100105958", "10", "MAT1", "1300", "2"), "Elikz": "X"}
    entries = _po_line_ledger([contract, closed])
    assert {e.procurement_status for e in entries.values()} == {ProcurementStatus.OPEN}

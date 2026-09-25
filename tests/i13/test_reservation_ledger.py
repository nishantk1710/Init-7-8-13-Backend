"""W6.2: Reservation -> PR -> PO -> GR -> GI, extending W6.1.

Pure unit tests against small fake repositories -- no CSV, no database, no
generated production data. Postgres query-shape/real-data tests live in
``test_reservation_ledger_postgres.py``.
"""

from datetime import date
from decimal import Decimal

from app.initiatives.i13.models import GrLinkStatus, LifecycleStatus, ReservationPrLinkStatus
from app.initiatives.i13.reservation_ledger import build_reservation_ledger
from app.shared.material_scope import MaterialScope


def _reservation(rsnum, rspos, material, plant, qty, banfn=None, bnfpo=None):
    return {
        "Rsnum": rsnum, "Rspos": rspos, "Matnr": material, "Werks": plant,
        "Bdmng": Decimal(qty), "Bdter": date(2026, 1, 1), "Banfn": banfn, "Bnfpo": bnfpo,
    }


def _gi(rsnum, rspos, bwart, qty, day):
    return {"Rsnum": rsnum, "Rspos": rspos, "Bwart": bwart, "Menge": Decimal(qty), "BudatMkpf": date(2026, 2, day)}


def _pr(banfn, bnfpo, material, plant, qty):
    return {"Banfn": banfn, "Bnfpo": bnfpo, "Matnr": material, "Werks": plant, "Menge": Decimal(qty), "Badat": date(2026, 1, 1)}


def _po(ebeln, ebelp, material, plant, qty, banfn=None, bnfpo=None):
    return {"Ebeln": ebeln, "Ebelp": ebelp, "Matnr": material, "Werks": plant, "Menge": Decimal(qty), "Banfn": banfn, "Bnfpo": bnfpo}


def _gr(ebeln, ebelp, bwart, qty, day):
    return {"Ebeln": ebeln, "Ebelp": ebelp, "Bwart": bwart, "Menge": Decimal(qty), "BudatMkpf": date(2026, 1, 1 + day)}


class _FakeReservationRepository:
    def __init__(self, reservations=(), gi_rows=()):
        self._reservations = list(reservations)
        self._gi_rows = list(gi_rows)

    def get_reservations(self, *, reservation_number=None, pr_number=None, material=None, plant=None):
        rows = self._reservations
        if reservation_number:
            rows = [r for r in rows if r["Rsnum"] == reservation_number]
        if pr_number:
            rows = [r for r in rows if r.get("Banfn") == pr_number]
        if material:
            rows = [r for r in rows if r["Matnr"] == material]
        if plant:
            rows = [r for r in rows if r["Werks"] == plant]
        return rows

    def get_goods_issue_by_reservation(self, *, reservation_number=None):
        rows = self._gi_rows
        if reservation_number:
            rows = [r for r in rows if r["Rsnum"] == reservation_number]
        return rows


class _FakeProcurementRepository:
    def __init__(self, prs=(), po_items=(), gr_rows=()):
        self._prs = list(prs)
        self._po_items = list(po_items)
        self._gr_rows = list(gr_rows)

    def get_purchase_requisitions(self, *, pr_number=None, material=None, plant=None):
        rows = self._prs
        if pr_number:
            rows = [r for r in rows if r["Banfn"] == pr_number]
        if material:
            rows = [r for r in rows if r["Matnr"] == material]
        if plant:
            rows = [r for r in rows if r["Werks"] == plant]
        return rows

    def get_purchase_order_items(self, *, po_number=None, pr_number=None, material=None, plant=None):
        rows = self._po_items
        if pr_number:
            rows = [r for r in rows if r.get("Banfn") == pr_number]
        if material:
            rows = [r for r in rows if r["Matnr"] == material]
        if plant:
            rows = [r for r in rows if r["Werks"] == plant]
        return rows

    def get_goods_receipt_history(self, *, po_number=None):
        return self._gr_rows

    def get_deterministic_gi_candidates(self, *, po_number=None):
        return []


def _oar_scope_index(*keys):
    """(material, plant) -> "ND" (an OAR MRP type) for every key given."""
    return {key: "ND" for key in keys}


def _build(reservations=(), gi_rows=(), prs=(), po_items=(), gr_rows=(), scope_index=None, **kwargs):
    reservation_repo = _FakeReservationRepository(reservations, gi_rows)
    procurement_repo = _FakeProcurementRepository(prs, po_items, gr_rows)
    scope_index = scope_index if scope_index is not None else _oar_scope_index(
        *{(r["Matnr"], r["Werks"]) for r in reservations}
    )
    return build_reservation_ledger(
        reservation_repo, procurement_repo, material_scope_index=scope_index, **kwargs
    )


# --- Reservation -> PR --------------------------------------------------


def test_reservation_links_to_pr_via_explicit_reference() -> None:
    entries = _build(
        reservations=[_reservation("50001", "1", "MAT1", "1300", "5", banfn="2000000001", bnfpo="10")],
        prs=[_pr("2000000001", "10", "MAT1", "1300", "5")],
        po_items=[_po("4500000001", "10", "MAT1", "1300", "5", banfn="2000000001", bnfpo="10")],
        gr_rows=[_gr("4500000001", "10", "101", "5", 1)],
    )
    assert len(entries) == 1
    assert entries[0].reservation_pr_link_status is ReservationPrLinkStatus.LINKED
    assert entries[0].po_number == "4500000001"
    assert entries[0].received_quantity == Decimal("5")


def test_reservation_with_no_pr_reference() -> None:
    entries = _build(reservations=[_reservation("50002", "1", "MAT1", "1300", "5")])
    assert len(entries) == 1
    assert entries[0].reservation_pr_link_status is ReservationPrLinkStatus.NO_PR_REFERENCE
    assert entries[0].po_number is None
    assert entries[0].received_quantity is None


def test_reservation_pr_reference_not_in_loaded_pr_set_is_unresolved() -> None:
    entries = _build(
        reservations=[_reservation("50003", "1", "MAT1", "1300", "5", banfn="2000000099", bnfpo="10")],
    )
    assert entries[0].reservation_pr_link_status is ReservationPrLinkStatus.PR_REFERENCE_UNRESOLVED
    assert entries[0].procurement_issued_quantity is None
    assert entries[0].direct_store_issued_quantity is None


def test_mrp_consolidation_is_reported_unresolved_not_split() -> None:
    """Two reservations pointing at the same PR item -- no approved
    allocation rule exists, so both must be CONSOLIDATION_UNRESOLVED, and
    neither may claim the shared PR's quantities."""
    entries = _build(
        reservations=[
            _reservation("50010", "1", "MAT1", "1300", "2", banfn="2000000010", bnfpo="10"),
            _reservation("50011", "1", "MAT1", "1300", "3", banfn="2000000010", bnfpo="10"),
        ],
        prs=[_pr("2000000010", "10", "MAT1", "1300", "5")],
        po_items=[_po("4500000010", "10", "MAT1", "1300", "5", banfn="2000000010", bnfpo="10")],
        gr_rows=[_gr("4500000010", "10", "101", "5", 1)],
    )
    assert len(entries) == 2
    for entry in entries:
        assert entry.reservation_pr_link_status is ReservationPrLinkStatus.CONSOLIDATION_UNRESOLVED
        assert entry.po_number is None, "must not guess which reservation owns the shared PR's PO"
        assert entry.received_quantity is None
        assert entry.procurement_issued_quantity is None


def test_reservation_split_across_multiple_pos_produces_multiple_entries() -> None:
    """Reuses W6.1's own multi-sourcing finding: one PR split across two
    POs must surface as two entries under the same reservation, not one."""
    entries = _build(
        reservations=[_reservation("50020", "1", "MAT1", "1300", "20", banfn="2000000020", bnfpo="10")],
        prs=[_pr("2000000020", "10", "MAT1", "1300", "20")],
        po_items=[
            _po("4500000020", "10", "MAT1", "1300", "12", banfn="2000000020", bnfpo="10"),
            _po("4500000021", "10", "MAT1", "1300", "8", banfn="2000000020", bnfpo="10"),
        ],
    )
    assert len(entries) == 2
    assert {e.po_number for e in entries} == {"4500000020", "4500000021"}
    assert all(e.reservation_number == "50020" for e in entries)


# --- Goods issue -> Reservation (the new deterministic link) ---------------


def test_gi_resolves_via_reservation_where_w61_could_not() -> None:
    entries = _build(
        reservations=[_reservation("50030", "1", "MAT1", "1300", "10", banfn="2000000030", bnfpo="10")],
        prs=[_pr("2000000030", "10", "MAT1", "1300", "10")],
        po_items=[_po("4500000030", "10", "MAT1", "1300", "10", banfn="2000000030", bnfpo="10")],
        gr_rows=[_gr("4500000030", "10", "101", "10", 1)],
        gi_rows=[_gi("50030", "1", "201", "4", 1), _gi("50030", "1", "261", "2", 5)],
    )
    entry = entries[0]
    assert entry.issued_quantity == Decimal("6")
    assert entry.lifecycle_status is LifecycleStatus.PARTIALLY_ISSUED


def test_no_issue_yet_is_zero_not_unresolved() -> None:
    entries = _build(
        reservations=[_reservation("50031", "1", "MAT1", "1300", "10", banfn="2000000031", bnfpo="10")],
        prs=[_pr("2000000031", "10", "MAT1", "1300", "10")],
        po_items=[_po("4500000031", "10", "MAT1", "1300", "10", banfn="2000000031", bnfpo="10")],
        gr_rows=[_gr("4500000031", "10", "101", "10", 1)],
    )
    entry = entries[0]
    assert entry.issued_quantity == Decimal("0")
    assert entry.gi_link_status.value == "LINKED"
    assert entry.lifecycle_status is LifecycleStatus.RECEIVED


def test_gi_reversal_via_reservation_nets_correctly() -> None:
    entries = _build(
        reservations=[_reservation("50032", "1", "MAT1", "1300", "10", banfn="2000000032", bnfpo="10")],
        prs=[_pr("2000000032", "10", "MAT1", "1300", "10")],
        po_items=[_po("4500000032", "10", "MAT1", "1300", "10", banfn="2000000032", bnfpo="10")],
        gr_rows=[_gr("4500000032", "10", "101", "10", 1)],
        gi_rows=[_gi("50032", "1", "201", "5", 1), _gi("50032", "1", "202", "2", 3)],
    )
    assert entries[0].issued_quantity == Decimal("3")


def test_no_gi_matching_by_material_plant_alone() -> None:
    """A GI row for an unrelated reservation, same material/plant, must
    never attribute to this reservation."""
    entries = _build(
        reservations=[_reservation("50033", "1", "MAT1", "1300", "10", banfn="2000000033", bnfpo="10")],
        prs=[_pr("2000000033", "10", "MAT1", "1300", "10")],
        po_items=[_po("4500000033", "10", "MAT1", "1300", "10", banfn="2000000033", bnfpo="10")],
        gr_rows=[_gr("4500000033", "10", "101", "10", 1)],
        gi_rows=[_gi("99999999", "9", "201", "7", 1)],
    )
    assert entries[0].issued_quantity == Decimal("0")


# --- Partial availability (direct-store vs. procurement split) -------------


def test_direct_store_and_procurement_issue_split() -> None:
    """reservation qty 10; 4 issued directly (no procurement backing),
    6 procured & received & issued -- must not claim all 10 as procured."""
    entries = _build(
        reservations=[_reservation("50040", "1", "MAT1", "1300", "10", banfn="2000000040", bnfpo="10")],
        prs=[_pr("2000000040", "10", "MAT1", "1300", "6")],
        po_items=[_po("4500000040", "10", "MAT1", "1300", "6", banfn="2000000040", bnfpo="10")],
        gr_rows=[_gr("4500000040", "10", "101", "6", 1)],
        gi_rows=[_gi("50040", "1", "201", "10", 5)],
    )
    entry = entries[0]
    assert entry.issued_quantity == Decimal("10")
    assert entry.received_quantity == Decimal("6")
    assert entry.procurement_issued_quantity == Decimal("6")
    assert entry.direct_store_issued_quantity == Decimal("4")


def test_partially_issued_with_no_procurement_compares_against_reservation_quantity() -> None:
    """No PR/PO at all, and issued_quantity < reservation_quantity -- must
    read PARTIALLY_ISSUED, not ISSUED (there is no received_quantity to
    compare against, so the reservation's own requested quantity is used)."""
    entries = _build(
        reservations=[_reservation("50042", "1", "MAT1", "1300", "100")],
        gi_rows=[_gi("50042", "1", "201", "40", 1)],
    )
    entry = entries[0]
    assert entry.issued_quantity == Decimal("40")
    assert entry.direct_store_issued_quantity == Decimal("40")
    assert entry.lifecycle_status is LifecycleStatus.PARTIALLY_ISSUED


def test_all_direct_store_when_no_procurement_at_all() -> None:
    entries = _build(
        reservations=[_reservation("50041", "1", "MAT1", "1300", "4")],
        gi_rows=[_gi("50041", "1", "201", "4", 1)],
    )
    entry = entries[0]
    assert entry.reservation_pr_link_status is ReservationPrLinkStatus.NO_PR_REFERENCE
    assert entry.procurement_issued_quantity == Decimal("0")
    assert entry.direct_store_issued_quantity == Decimal("4")
    assert entry.lifecycle_status is LifecycleStatus.ISSUED


# --- OAR scope (W2.4) --------------------------------------------------


def test_non_oar_material_excluded_by_default() -> None:
    entries = _build(
        reservations=[_reservation("50050", "1", "MAT-VB", "1300", "5")],
        scope_index={("MAT-VB", "1300"): "VB"},
    )
    assert entries == []


def test_include_out_of_scope_returns_non_oar_materials() -> None:
    entries = _build(
        reservations=[_reservation("50051", "1", "MAT-VB", "1300", "5")],
        scope_index={("MAT-VB", "1300"): "VB"},
        include_out_of_scope=True,
    )
    assert len(entries) == 1
    assert entries[0].material_scope is MaterialScope.MIN_MAX


def test_oar_material_included_by_default() -> None:
    entries = _build(
        reservations=[_reservation("50052", "1", "MAT-ND", "1300", "5")],
        scope_index={("MAT-ND", "1300"): "ND"},
    )
    assert len(entries) == 1
    assert entries[0].material_scope is MaterialScope.OAR

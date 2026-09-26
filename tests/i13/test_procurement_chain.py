"""W6.1: deterministic PR -> PO -> GR -> GI stitching, without the
reservation leg.

Pure unit tests against a small fake repository -- no CSV, no database, no
generated production data. Postgres query-shape/real-data tests live in
``test_procurement_chain_postgres.py``, skipped when no database is
configured.
"""

from datetime import date
from decimal import Decimal

from app.initiatives.i13.models import GiLinkStatus, GrLinkStatus, LifecycleStatus, PrPoLinkStatus
from app.initiatives.i13.procurement_chain import build_procurement_chain, compute_chain_diagnostics


def _pr(banfn, bnfpo, material, plant, qty):
    return {"Banfn": banfn, "Bnfpo": bnfpo, "Matnr": material, "Werks": plant, "Menge": Decimal(qty), "Badat": date(2026, 1, 1)}


def _po(ebeln, ebelp, material, plant, qty, banfn=None, bnfpo=None):
    return {
        "Ebeln": ebeln, "Ebelp": ebelp, "Matnr": material, "Werks": plant, "Menge": Decimal(qty),
        "Banfn": banfn, "Bnfpo": bnfpo,
    }


def _gr(ebeln, ebelp, bwart, qty, days_ago):
    return {"Ebeln": ebeln, "Ebelp": ebelp, "Bwart": bwart, "Menge": Decimal(qty), "BudatMkpf": date(2026, 1, 1 + days_ago)}


def _gi(ebeln, ebelp, bwart, qty, day):
    return {"Ebeln": ebeln, "Ebelp": ebelp, "Bwart": bwart, "Menge": Decimal(qty), "BudatMkpf": date(2026, 2, day)}


class _FakeRepository:
    """Stands in for PostgresProcurementRepository -- proves the stitching
    logic. The real repository's query shape is proven separately, against
    Postgres, in test_procurement_chain_postgres.py."""

    def __init__(self, prs=(), po_items=(), gr_rows=(), gi_rows=()):
        self._prs = list(prs)
        self._po_items = list(po_items)
        self._gr_rows = list(gr_rows)
        self._gi_rows = list(gi_rows)

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
        if po_number:
            rows = [r for r in rows if r["Ebeln"] == po_number]
        if pr_number:
            rows = [r for r in rows if r.get("Banfn") == pr_number]
        if material:
            rows = [r for r in rows if r["Matnr"] == material]
        if plant:
            rows = [r for r in rows if r["Werks"] == plant]
        return rows

    def get_goods_receipt_history(self, *, po_number=None):
        rows = self._gr_rows
        if po_number:
            rows = [r for r in rows if r["Ebeln"] == po_number]
        return rows

    def get_deterministic_gi_candidates(self, *, po_number=None):
        rows = self._gi_rows
        if po_number:
            rows = [r for r in rows if r["Ebeln"] == po_number]
        return rows


# --- PR -> PO stitching ------------------------------------------------


def test_pr_links_to_po_via_explicit_reference() -> None:
    repo = _FakeRepository(
        prs=[_pr("2000000001", "10", "MAT1", "1300", "5")],
        po_items=[_po("4500000001", "10", "MAT1", "1300", "5", banfn="2000000001", bnfpo="10")],
    )
    entries = build_procurement_chain(repo)
    assert len(entries) == 1
    assert entries[0].pr_number == "2000000001"
    assert entries[0].po_number == "4500000001"
    assert entries[0].pr_po_link_status is PrPoLinkStatus.LINKED


def test_pr_with_no_po_remains_pr_only() -> None:
    repo = _FakeRepository(prs=[_pr("2000000002", "10", "MAT1", "1300", "5")])
    entries = build_procurement_chain(repo)
    assert len(entries) == 1
    assert entries[0].po_number is None
    assert entries[0].pr_po_link_status is PrPoLinkStatus.NO_PO_YET
    assert entries[0].lifecycle_status is LifecycleStatus.PR_CREATED


def test_po_with_pr_reference_not_in_loaded_pr_set_is_unresolved_not_dropped() -> None:
    """A PO carries a PR reference, but that PR isn't in the loaded EBAN
    extract (measured real gap: ~31% of cases, e.g. plant-coverage limits).
    Must be reported unresolved, never silently matched or discarded."""
    repo = _FakeRepository(
        po_items=[_po("4500000002", "10", "MAT1", "1300", "5", banfn="2000000099", bnfpo="10")],
    )
    entries = build_procurement_chain(repo)
    assert len(entries) == 1
    assert entries[0].pr_number == "2000000099"
    assert entries[0].pr_po_link_status is PrPoLinkStatus.PR_REFERENCE_UNRESOLVED
    assert entries[0].pr_quantity is None


def test_po_with_no_pr_reference_at_all() -> None:
    repo = _FakeRepository(po_items=[_po("4500000003", "10", "MAT1", "1300", "5")])
    entries = build_procurement_chain(repo)
    assert entries[0].pr_po_link_status is PrPoLinkStatus.NO_PR_REFERENCE


def test_multiple_po_lines_do_not_cross_link() -> None:
    """Two distinct PR items, each with its own PO -- must not mix."""
    repo = _FakeRepository(
        prs=[_pr("2000000010", "10", "MAT1", "1300", "5"), _pr("2000000011", "10", "MAT2", "1300", "8")],
        po_items=[
            _po("4500000010", "10", "MAT1", "1300", "5", banfn="2000000010", bnfpo="10"),
            _po("4500000011", "10", "MAT2", "1300", "8", banfn="2000000011", bnfpo="10"),
        ],
    )
    entries = {e.po_number: e for e in build_procurement_chain(repo)}
    assert entries["4500000010"].pr_number == "2000000010"
    assert entries["4500000011"].pr_number == "2000000011"
    assert entries["4500000010"].material == "MAT1"
    assert entries["4500000011"].material == "MAT2"


def test_different_plants_do_not_cross_link() -> None:
    """Same material, different plants, unrelated PR/PO pairs."""
    repo = _FakeRepository(
        prs=[_pr("2000000020", "10", "MAT1", "1300", "5"), _pr("2000000021", "10", "MAT1", "1500", "8")],
        po_items=[
            _po("4500000020", "10", "MAT1", "1300", "5", banfn="2000000020", bnfpo="10"),
            _po("4500000021", "10", "MAT1", "1500", "8", banfn="2000000021", bnfpo="10"),
        ],
    )
    entries = {e.plant: e for e in build_procurement_chain(repo)}
    assert entries["1300"].pr_number == "2000000020"
    assert entries["1500"].pr_number == "2000000021"


def test_pr_item_legitimately_split_across_multiple_pos() -> None:
    """Real multi-sourcing: one PR item, two different POs -- both must
    appear as separate entries, not collapsed or treated as ambiguous."""
    repo = _FakeRepository(
        prs=[_pr("2000000030", "10", "MAT1", "1300", "20")],
        po_items=[
            _po("4500000030", "10", "MAT1", "1300", "12", banfn="2000000030", bnfpo="10"),
            _po("4500000031", "10", "MAT1", "1300", "8", banfn="2000000030", bnfpo="10"),
        ],
    )
    entries = build_procurement_chain(repo)
    assert len(entries) == 2
    assert {e.po_number for e in entries} == {"4500000030", "4500000031"}
    assert all(e.pr_number == "2000000030" for e in entries)


# --- PO -> GR ------------------------------------------------------------


def test_po_with_single_gr() -> None:
    repo = _FakeRepository(
        po_items=[_po("4500000040", "10", "MAT1", "1300", "10")],
        gr_rows=[_gr("4500000040", "10", "101", "10", 5)],
    )
    entry = build_procurement_chain(repo)[0]
    assert entry.received_quantity == Decimal("10")
    assert entry.gr_link_status is GrLinkStatus.RECEIVED
    assert entry.first_gr_date == entry.last_gr_date == date(2026, 1, 6)


def test_po_with_multiple_partial_grs_aggregates() -> None:
    repo = _FakeRepository(
        po_items=[_po("4500000041", "10", "MAT1", "1300", "10")],
        gr_rows=[_gr("4500000041", "10", "101", "4", 1), _gr("4500000041", "10", "101", "3", 10)],
    )
    entry = build_procurement_chain(repo)[0]
    assert entry.received_quantity == Decimal("7")
    assert entry.first_gr_date == date(2026, 1, 2)
    assert entry.last_gr_date == date(2026, 1, 11)


def test_gr_reversal_nets_correctly() -> None:
    """+5 then a -2 reversal must net to 3, not 5 and not 7 -- reuses
    movements.py's shared reversal rule (102 reverses 101)."""
    repo = _FakeRepository(
        po_items=[_po("4500000042", "10", "MAT1", "1300", "10")],
        gr_rows=[_gr("4500000042", "10", "101", "5", 1), _gr("4500000042", "10", "102", "2", 3)],
    )
    entry = build_procurement_chain(repo)[0]
    assert entry.received_quantity == Decimal("3")


def test_po_with_no_gr_reports_zero_receipt() -> None:
    repo = _FakeRepository(po_items=[_po("4500000043", "10", "MAT1", "1300", "10")])
    entry = build_procurement_chain(repo)[0]
    assert entry.received_quantity == Decimal("0")
    assert entry.gr_link_status is GrLinkStatus.NO_RECEIPTS
    assert entry.first_gr_date is None
    assert entry.lifecycle_status is LifecycleStatus.ORDERED


# --- Goods issue linkage ---------------------------------------------------


def test_gi_linked_by_true_deterministic_key_is_aggregated() -> None:
    """If a deterministic PO-referencing issue movement DID exist (today it
    never does in real data -- see the module docstring), the aggregation
    logic itself must be correct."""
    repo = _FakeRepository(
        po_items=[_po("4500000050", "10", "MAT1", "1300", "10")],
        gr_rows=[_gr("4500000050", "10", "101", "10", 1)],
        gi_rows=[_gi("4500000050", "10", "201", "2", 1), _gi("4500000050", "10", "261", "3", 5)],
    )
    entry = build_procurement_chain(repo)[0]
    assert entry.issued_quantity == Decimal("5")
    assert entry.gi_link_status is GiLinkStatus.LINKED
    assert entry.lifecycle_status is LifecycleStatus.PARTIALLY_ISSUED


def test_gi_without_deterministic_key_remains_unresolved_not_zero() -> None:
    repo = _FakeRepository(
        po_items=[_po("4500000051", "10", "MAT1", "1300", "10")],
        gr_rows=[_gr("4500000051", "10", "101", "10", 1)],
    )
    entry = build_procurement_chain(repo)[0]
    assert entry.issued_quantity is None, "unresolved must not be faked as zero"
    assert entry.gi_link_status is GiLinkStatus.UNRESOLVED_PENDING_RESERVATION
    assert entry.gi_link_reason is not None
    # received >= ordered but GI unresolved -> honest RECEIVED, never ISSUED/UNISSUED
    assert entry.lifecycle_status is LifecycleStatus.RECEIVED


def test_no_gi_matching_by_material_plant_alone() -> None:
    """A GI candidate for the SAME material/plant but a DIFFERENT PO must
    never be attributed to this PO -- only an exact (Ebeln, Ebelp) match
    counts, never material/plant proximity."""
    repo = _FakeRepository(
        po_items=[_po("4500000052", "10", "MAT1", "1300", "10")],
        gr_rows=[_gr("4500000052", "10", "101", "10", 1)],
        gi_rows=[_gi("9999999999", "99", "201", "5", 1)],  # unrelated PO, same material/plant implied
    )
    entry = build_procurement_chain(repo)[0]
    assert entry.issued_quantity is None
    assert entry.gi_link_status is GiLinkStatus.UNRESOLVED_PENDING_RESERVATION


def test_multiple_issues_aggregate_without_fan_out() -> None:
    """2 GR rows x 2 GI rows must not multiply into 4x totals."""
    repo = _FakeRepository(
        po_items=[_po("4500000053", "10", "MAT1", "1300", "10")],
        gr_rows=[_gr("4500000053", "10", "101", "4", 1), _gr("4500000053", "10", "101", "6", 2)],
        gi_rows=[_gi("4500000053", "10", "201", "2", 1), _gi("4500000053", "10", "201", "3", 2)],
    )
    entry = build_procurement_chain(repo)[0]
    assert entry.received_quantity == Decimal("10")
    assert entry.issued_quantity == Decimal("5")


# --- Lifecycle status -------------------------------------------------


def test_lifecycle_ordered_when_zero_received() -> None:
    repo = _FakeRepository(po_items=[_po("4500000060", "10", "MAT1", "1300", "10")])
    assert build_procurement_chain(repo)[0].lifecycle_status is LifecycleStatus.ORDERED


def test_lifecycle_partially_received() -> None:
    repo = _FakeRepository(
        po_items=[_po("4500000061", "10", "MAT1", "1300", "10")],
        gr_rows=[_gr("4500000061", "10", "101", "4", 1)],
    )
    assert build_procurement_chain(repo)[0].lifecycle_status is LifecycleStatus.PARTIALLY_RECEIVED


def test_lifecycle_issued_when_fully_issued() -> None:
    repo = _FakeRepository(
        po_items=[_po("4500000062", "10", "MAT1", "1300", "10")],
        gr_rows=[_gr("4500000062", "10", "101", "10", 1)],
        gi_rows=[_gi("4500000062", "10", "201", "10", 1)],
    )
    assert build_procurement_chain(repo)[0].lifecycle_status is LifecycleStatus.ISSUED


# --- Diagnostics ------------------------------------------------------


def test_diagnostics_report_duplicate_source_keys() -> None:
    repo = _FakeRepository(
        prs=[_pr("2000000070", "10", "MAT1", "1300", "5"), _pr("2000000070", "10", "MAT1", "1300", "5")],
        po_items=[_po("4500000070", "10", "MAT1", "1300", "5", banfn="2000000070", bnfpo="10")],
    )
    diagnostics = compute_chain_diagnostics(repo)
    assert ("2000000070", "10") in diagnostics.duplicate_pr_keys


def test_diagnostics_report_split_pr_and_unresolved_references() -> None:
    repo = _FakeRepository(
        prs=[_pr("2000000080", "10", "MAT1", "1300", "20")],
        po_items=[
            _po("4500000080", "10", "MAT1", "1300", "12", banfn="2000000080", bnfpo="10"),
            _po("4500000081", "10", "MAT1", "1300", "8", banfn="2000000080", bnfpo="10"),
            _po("4500000082", "10", "MAT1", "1300", "3", banfn="2000000999", bnfpo="10"),
        ],
    )
    diagnostics = compute_chain_diagnostics(repo)
    assert diagnostics.pr_items_with_multiple_po == 1
    assert diagnostics.po_items_with_unresolved_pr_reference == 1
    # The PO referencing an unresolved PR key must not also be counted as if
    # it were a real, loaded PR item -- the three buckets must sum exactly
    # to pr_items_total (regression: they didn't, until fixed).
    assert (
        diagnostics.pr_items_with_no_po + diagnostics.pr_items_with_single_po + diagnostics.pr_items_with_multiple_po
        == diagnostics.pr_items_total
        == 1
    )

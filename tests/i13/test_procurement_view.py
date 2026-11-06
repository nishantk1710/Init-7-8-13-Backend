"""The procurement lines screen shows only what SAP recorded
(``ledger_compat.procurement_view``, 5-Nov-2026): a line a reservation raised
keeps its exact issues; a stock refill's estimated issues are left off."""

from datetime import date
from decimal import Decimal

from app.api.i13.ledger import _matching
from app.initiatives.i13.ledger_compat import (
    LedgerUtilisationStatus,
    build_legacy_ledger,
    procurement_view,
)
from app.initiatives.i13.snapshot_store import kinds
from app.initiatives.i13.stock_issue_matching import IssueMatch
from tests.i13.conftest import (
    FakeMovementRepository,
    FakeProcurementRepository,
    FakeReservationRepository,
    oar_scope_index,
)
from tests.i13.test_ledger_compat import _gi, _gr, _po, _pr, _reservation


def _ledger():
    """Two PO lines of MAT1/1300, both received 5 on 2 Jan:

    * 4500000001 -- its PR was raised by reservation 1000000000/0001, which
      had 3 issued against it;
    * 4500000002 -- a stock refill; 4 of the material were issued in
      February against other reservations, so the oldest-first estimate
      counts some of them against this line.
    """
    procurement = FakeProcurementRepository(
        prs=[_pr("2000000001", "10", "MAT1", "1300", "5"), _pr("2000000002", "10", "MAT1", "1300", "5")],
        po_items=[
            _po("4500000001", "10", "MAT1", "1300", "5", banfn="2000000001", bnfpo="10"),
            _po("4500000002", "10", "MAT1", "1300", "5", banfn="2000000002", bnfpo="10"),
        ],
        gr_rows=[_gr("4500000001", "10", "101", "5", 1), _gr("4500000002", "10", "101", "5", 1)],
    )
    reservations = FakeReservationRepository(
        reservations=[_reservation("1000000000", "0001", "MAT1", "1300", "2000000001", "10")],
        gi_rows=[_gi("1000000000", "0001", "261", "3", 1)],
    )
    movements = FakeMovementRepository(
        movements=[
            {"Matnr": "MAT1", "Werks": "1300", "Bwart": "101", "Menge": Decimal("10"), "BudatMkpf": date(2026, 1, 2)},
            {"Matnr": "MAT1", "Werks": "1300", "Bwart": "261", "Menge": Decimal("3"), "BudatMkpf": date(2026, 2, 1),
             "Rsnum": "1000000000", "Rspos": "0001"},
            {"Matnr": "MAT1", "Werks": "1300", "Bwart": "261", "Menge": Decimal("4"), "BudatMkpf": date(2026, 2, 3),
             "Rsnum": "1000000099", "Rspos": "0001"},
        ],
    )
    entries = build_legacy_ledger(
        procurement, reservations, oar_scope_index(("MAT1", "1300")), plant="1300", movement_repository=movements
    )
    return {e.po_number: e for e in entries}


def test_a_line_a_reservation_raised_keeps_its_exact_issues() -> None:
    line = _ledger()["4500000001"]
    shown = procurement_view(line)
    assert shown == line
    assert shown.issued_quantity == Decimal("3")
    assert shown.utilisation_status is LedgerUtilisationStatus.PARTIALLY_ISSUED


def test_a_stock_refill_shows_its_receipt_and_no_estimated_issue() -> None:
    line = _ledger()["4500000002"]
    assert line.issue_match is IssueMatch.OLDEST_DELIVERY_FIRST  # the stored line keeps the estimate
    shown = procurement_view(line)
    assert shown.utilisation_status is LedgerUtilisationStatus.NOT_TRACKED
    assert shown.issued_quantity == 0
    assert (shown.first_gi_date, shown.latest_gi_date) == (None, None)
    assert (shown.next_planned_issue_date, shown.next_planned_issue_quantity) == (None, None)
    assert shown.received_quantity == Decimal("5") and shown.first_gr_date == line.first_gr_date


def test_the_utilisation_filter_reads_the_view_in_memory_and_stored() -> None:
    lines = list(_ledger().values())
    filters = dict(plant=None, material=None, procurement_status=None, delivery_status=None)
    assert [e.po_number for e in _matching(lines, utilisation_status="not_tracked", **filters)] == ["4500000002"]
    assert [e.po_number for e in _matching(lines, utilisation_status="PARTIALLY_ISSUED", **filters)] == ["4500000001"]
    assert {kinds.lledger(e, True).f2 for e in lines} == {"PARTIALLY_ISSUED", "NOT_TRACKED"}

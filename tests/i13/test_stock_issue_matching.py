"""Goods issued per PO line (stock_issue_matching): the reservation that raised
the PR where there is one, else stock issued out of the deliveries oldest first.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

from app.initiatives.i13.ledger_compat import LedgerUtilisationStatus, build_legacy_ledger, grni_lines
from app.initiatives.i13.stock_issue_matching import IssueMatch, match_po_line_issues, po_line_issues
from app.initiatives.i13.watch import compute_watch_metrics
from tests.i13.conftest import (
    FakeMovementRepository,
    FakeProcurementRepository,
    FakeReservationRepository,
    oar_scope_index,
    write_csv,
)
from tests.i13.test_watch import PLAN_HEADER

D = Decimal


def _po(ebeln, material="MAT1", plant="1300", qty="20", banfn=None, bnfpo=None):
    return {"Ebeln": ebeln, "Ebelp": "00010", "Matnr": material, "Werks": plant, "Menge": D(qty),
            "Banfn": banfn, "Bnfpo": bnfpo}


def _gr(ebeln, on, qty, bwart="101"):
    return {"Ebeln": ebeln, "Ebelp": "00010", "Bwart": bwart, "Menge": D(qty), "BudatMkpf": on}


def _move(bwart, on, qty, material="MAT1", plant="1300"):
    return {"Matnr": material, "Werks": plant, "Bwart": bwart, "Menge": D(qty), "BudatMkpf": on}


def _reservation(rsnum, needed_on, qty, withdrawn="0", banfn=None, bnfpo=None, **flags):
    return {"Rsnum": rsnum, "Rspos": "0001", "Matnr": "MAT1", "Werks": "1300", "Bdter": needed_on,
            "Bdmng": D(qty), "Enmng": D(withdrawn), "Banfn": banfn, "Bnfpo": bnfpo, "Bwart": "261",
            "Xloek": flags.get("deleted", False), "Kzear": flags.get("final", False)}


# PO-A delivers twice (1 Mar 10, 10 Apr 10), PO-B once (1 May 10); stock goes
# out 15 Mar 6, 20 Apr 8, 20 May 7; a reservation needs 5 more on 5 Jun.
PO_ITEMS = [_po("PO-A"), _po("PO-B", qty="10")]
GR_ROWS = [
    _gr("PO-A", date(2026, 3, 1), "10"), _gr("PO-A", date(2026, 4, 10), "10"), _gr("PO-B", date(2026, 5, 1), "10"),
]
ISSUES = [_move("261", date(2026, 3, 15), "6"), _move("261", date(2026, 4, 20), "8"), _move("261", date(2026, 5, 20), "7")]
HISTORY_START = date(2026, 1, 1)


def _match(movements=ISSUES, reservations=(), gi_by_reservation=(), po_items=PO_ITEMS, gr_rows=GR_ROWS,
           history_start=HISTORY_START):
    return match_po_line_issues(
        po_items=list(po_items), gr_rows=list(gr_rows), movements=list(movements),
        reservations=list(reservations), gi_by_reservation=list(gi_by_reservation), history_start=history_start,
    )


def test_issues_use_up_the_oldest_delivery_first() -> None:
    lines = _match(reservations=[_reservation("R1", date(2026, 6, 5), "5")])
    a, b = lines[("PO-A", "00010")], lines[("PO-B", "00010")]

    assert (a.received_quantity, a.issued_quantity, a.delivery_count) == (D("20"), D("20"), 2)
    assert (a.first_issue_date, a.last_issue_date) == (date(2026, 3, 15), date(2026, 5, 20))
    assert a.issue_match is IssueMatch.OLDEST_DELIVERY_FIRST
    assert a.next_planned_issue_date is None  # nothing left in stock to plan from

    assert (b.received_quantity, b.issued_quantity, b.delivery_count) == (D("10"), D("1"), 1)
    assert (b.first_issue_date, b.last_issue_date) == (date(2026, 5, 20), date(2026, 5, 20))
    assert (b.next_planned_issue_date, b.next_planned_issue_quantity) == (date(2026, 6, 5), D("5"))


def test_an_issue_never_uses_a_delivery_that_had_not_arrived() -> None:
    # An issue on 20 Feb, before any delivery arrived, came out of older stock.
    lines = _match(movements=[_move("261", date(2026, 2, 20), "6")])
    assert lines[("PO-A", "00010")].issued_quantity == 0
    assert lines[("PO-B", "00010")].issued_quantity == 0


def test_a_reversed_issue_is_put_back() -> None:
    lines = _match(movements=[_move("261", date(2026, 3, 15), "6"), _move("262", date(2026, 3, 16), "6")])
    a = lines[("PO-A", "00010")]
    assert a.issued_quantity == 0
    assert a.first_issue_date is None


def test_a_reversed_receipt_is_not_stock() -> None:
    lines = _match(
        gr_rows=[_gr("PO-A", date(2026, 3, 1), "10"), _gr("PO-A", date(2026, 3, 2), "10", bwart="102"),
                 _gr("PO-B", date(2026, 5, 1), "10")],
        movements=[_move("261", date(2026, 5, 20), "4")],
    )
    assert lines[("PO-A", "00010")].delivery_count == 0
    assert lines[("PO-B", "00010")].issued_quantity == D("4")


def test_a_pr_raised_by_a_reservation_takes_that_reservations_issues_only() -> None:
    po_items = [_po("PO-A"), _po("PO-D", qty="5", banfn="PR9", bnfpo="00010")]
    gr_rows = [*GR_ROWS[:2], _gr("PO-D", date(2026, 3, 1), "5")]
    direct = _reservation("R9", date(2026, 3, 10), "5", withdrawn="3", banfn="PR9", bnfpo="00010")
    gi = [{"Rsnum": "R9", "Rspos": "0001", "Bwart": "261", "Menge": D("3"), "BudatMkpf": date(2026, 3, 15)}]
    movements = [_move("261", date(2026, 3, 15), "3"), _move("261", date(2026, 3, 15), "6")]

    lines = _match(movements=movements, reservations=[direct], gi_by_reservation=gi, po_items=po_items, gr_rows=gr_rows)

    d = lines[("PO-D", "00010")]
    assert (d.issue_match, d.issued_quantity) == (IssueMatch.RESERVATION, D("3"))
    assert (d.next_planned_issue_date, d.next_planned_issue_quantity) == (date(2026, 3, 10), D("2"))
    # The reservation's 3 are not counted against the stock line again.
    assert lines[("PO-A", "00010")].issued_quantity == D("6")


def test_a_delivery_before_the_movement_history_is_not_called_unissued() -> None:
    lines = _match(history_start=date(2026, 4, 1))
    a = lines[("PO-A", "00010")]
    assert a.issue_match is IssueMatch.BEFORE_ISSUE_HISTORY
    assert a.issued_quantity == 0
    # Left out of the queue. PO-B (1 May) takes only what went out after it
    # arrived: the 20 May issue of 7.
    assert lines[("PO-B", "00010")].issued_quantity == D("7")


def test_closed_deleted_and_transfer_reservations_are_not_planned_issues() -> None:
    reservations = [
        _reservation("R1", date(2026, 6, 1), "5", withdrawn="5"),
        _reservation("R2", date(2026, 6, 2), "5", final=True),
        _reservation("R3", date(2026, 6, 3), "5", deleted=True),
        {**_reservation("R4", date(2026, 6, 4), "5"), "Bwart": "311"},
    ]
    assert _match(reservations=reservations)[("PO-B", "00010")].next_planned_issue_date is None


def test_a_line_with_nothing_delivered_has_no_match() -> None:
    lines = _match(po_items=[*PO_ITEMS, _po("PO-C")])
    assert lines[("PO-C", "00010")].issue_match is None
    assert lines[("PO-C", "00010")].issued_quantity == 0


# --- the ledger, GRNI and WATCH read the same matching ------------------------


def _repos(movements=ISSUES):
    return (
        FakeProcurementRepository(po_items=PO_ITEMS, gr_rows=GR_ROWS),
        FakeReservationRepository(reservations=[_reservation("R1", date(2026, 6, 5), "5")]),
        FakeMovementRepository(movements=[_move("101", HISTORY_START, "1"), *movements]),
    )


def test_the_ledger_shows_partially_and_fully_issued() -> None:
    procurement, reservations, movements = _repos()
    entries = {
        e.po_number: e
        for e in build_legacy_ledger(
            procurement, reservations, oar_scope_index(("MAT1", "1300")), movement_repository=movements
        )
    }
    assert entries["PO-A"].utilisation_status is LedgerUtilisationStatus.FULLY_ISSUED
    assert entries["PO-B"].utilisation_status is LedgerUtilisationStatus.PARTIALLY_ISSUED
    assert (entries["PO-B"].issued_quantity, entries["PO-B"].open_quantity) == (D("1"), D("9"))
    assert entries["PO-B"].issue_match is IssueMatch.OLDEST_DELIVERY_FIRST
    assert entries["PO-B"].latest_gi_date == date(2026, 5, 20)
    assert entries["PO-B"].next_planned_issue_date == date(2026, 6, 5)
    assert "oldest delivery first" in entries["PO-B"].attribution_evidence


def test_grni_is_the_po_lines_with_stock_left_past_the_threshold() -> None:
    procurement, reservations, movements = _repos()
    entries = build_legacy_ledger(
        procurement, reservations, oar_scope_index(("MAT1", "1300")), movement_repository=movements
    )
    found = [(e.po_number, left, age) for e, left, age in grni_lines(entries, as_of=date(2026, 6, 10), threshold_days=30)]
    assert found == [("PO-B", D("9"), 40)]


def test_watch_flags_received_stock_not_issued(data_dir: Path, i13_config) -> None:
    write_csv(data_dir / "platform" / "consumption_plans.csv", PLAN_HEADER, [])
    procurement, reservations, movements = _repos()
    metric = next(
        m
        for m in compute_watch_metrics(
            movements, procurement, reservations, oar_scope_index(("MAT1", "1300")), i13_config, data_dir,
            as_of=date(2026, 6, 10),
        )
        if m.material == "MAT1"
    )
    assert metric.gr_not_issued_flag is True
    assert metric.gr_not_issued_relevant_gr_date == date(2026, 5, 1)
    assert metric.gr_not_issued_outstanding_quantity == D("9")


def test_one_material_matches_as_it_does_inside_the_whole() -> None:
    procurement, reservations, movements = _repos([*ISSUES, _move("261", date(2026, 5, 20), "3", material="MAT2")])
    whole = po_line_issues(procurement, reservations, movements, history_start=HISTORY_START)
    one = po_line_issues(procurement, reservations, movements, material="MAT1", plant="1300", history_start=HISTORY_START)
    assert one == {k: v for k, v in whole.items() if v.material == "MAT1"}

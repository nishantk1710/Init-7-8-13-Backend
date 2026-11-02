"""A reservation's issues carry their reversals (202/262), so the issued
quantity on the Reservations tab nets them out.

Both readers of the issue-by-reservation link are covered: the snapshot
batch's single pass over the movement work table, and the SQL each one sends.
"""

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.initiatives.i13.movements import ISSUE_TYPES, net_quantity, net_standing_quantity
from app.initiatives.i13.snapshot_store.batch_repos import Batch, BatchMovements
from app.initiatives.i13.snapshot_store.work_tables import WorkTables
from app.integrations.sap.postgres_reservation import _GI_BY_RESERVATION_QUERY


def _mov(movement_type, quantity, *, reservation="", purchase_order="", item=""):
    return SimpleNamespace(
        material="MAT1", plant="1300", movement_type=movement_type, quantity=quantity,
        posting_date="20260201", purchase_order=purchase_order, item=item,
        reservation=reservation, item_no_stock_transfer_reserv="1" if reservation else "",
    )


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *_args, **_kwargs):
        return iter(self._rows)


def _movements(rows):
    return BatchMovements(db=_FakeSession(rows), work=WorkTables(0), batch=Batch("MAT0", "MAT9"))


def test_a_reversed_issue_nets_out_of_the_reservation() -> None:
    movements = _movements([
        _mov("261", "5", reservation="0010000001"),
        _mov("262", "5", reservation="0010000001"),
        _mov("261", "2", reservation="0010000001"),
    ])
    rows = movements.issues_by_reservation()
    assert sorted(r["Bwart"] for r in rows) == ["261", "261", "262"]
    assert net_standing_quantity(rows, ISSUE_TYPES) == Decimal("2")


def test_po_issues_stay_issues_only() -> None:
    movements = _movements([
        _mov("201", "3", reservation="0010000002", purchase_order="4500000001", item="10"),
        _mov("202", "3", reservation="0010000002", purchase_order="4500000001", item="10"),
    ])
    assert [r["Bwart"] for r in movements.issues_by_po()] == ["201"]
    assert sorted(r["Bwart"] for r in movements.issues_by_reservation()) == ["201", "202"]


def test_rows_without_a_reservation_are_left_out() -> None:
    movements = _movements([_mov("262", "1"), _mov("262", "1", reservation="0")])
    assert movements.issues_by_reservation() == []


def test_the_live_query_selects_the_reversals_too() -> None:
    assert "IN ('201', '261', '202', '262')" in _GI_BY_RESERVATION_QUERY


def _row(bwart, qty, month):
    return {"Bwart": bwart, "Menge": Decimal(qty), "BudatMkpf": date(2026, month, 1)}


def test_a_reversal_whose_issue_predates_the_history_takes_nothing_back() -> None:
    # The 262 in January reverses an issue the extract does not hold.
    rows = [_row("262", "3", 1), _row("261", "2", 2)]
    assert net_quantity(rows, ISSUE_TYPES) == Decimal("-1")
    assert net_standing_quantity(rows, ISSUE_TYPES) == Decimal("2")


def test_a_reversal_takes_back_only_what_was_issued_before_it() -> None:
    rows = [_row("261", "2", 3), _row("262", "5", 4), _row("261", "1", 5)]
    assert net_standing_quantity(rows, ISSUE_TYPES) == Decimal("1")


def test_reversal_dates_do_not_become_issue_dates() -> None:
    from app.initiatives.i13.movements import event_dates

    rows = [
        {"Bwart": "261", "Menge": Decimal("1"), "BudatMkpf": date(2026, 2, 1)},
        {"Bwart": "262", "Menge": Decimal("1"), "BudatMkpf": date(2026, 3, 1)},
    ]
    assert event_dates(rows, ISSUE_TYPES) == (date(2026, 2, 1), date(2026, 2, 1))

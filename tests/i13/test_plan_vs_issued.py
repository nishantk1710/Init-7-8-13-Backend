"""The acquired-vs-plan tab's rows (``plan_vs_issued``): every captured plan,
and every OAR reservation raised directly in SAP that no plan owns."""

from datetime import date
from decimal import Decimal

from app.api.i13.plan_vs_issued import _matches, _response, _with_session
from app.initiatives.i13.plan_vs_issued import (
    RowSource,
    RowStatus,
    from_payload,
    is_overdue,
    plan_vs_issued_rows,
    to_payload,
)
from app.initiatives.i13.plans import PlanSource
from app.initiatives.i13.snapshot_store import kinds
from app.shared.material_scope import MaterialScope
from tests.i13.test_plan_outcomes import _captured, _entry, _linked

TODAY = date(2026, 11, 2)
PAST = date(2026, 10, 15)
AHEAD = date(2026, 12, 1)


def _rows(plans, entries):
    return list(plan_vs_issued_rows(plans, entries, as_of=TODAY))


def _sap(rspos: str, *, reserved: str = "10", issued: str = "0", required: date | None = PAST, **overrides):
    return _entry(
        rspos, issued, reservation_quantity=Decimal(reserved), requirement_date=required,
        material="MAT9", **overrides,
    )


def _status(rows, row_id):
    return next(r for r in rows if r.row_id == row_id).status


# --- one rule for both sources: issued against planned (or reserved) ----------


def test_sap_rows_compare_issued_with_reserved() -> None:
    rows = _rows([], [
        _sap("0001", issued="10"),
        _sap("0002", issued="4"),
        _sap("0003", issued="15"),
        _sap("0004", issued="0"),
        _sap("0005", issued="0", required=AHEAD),
    ])
    assert {r.reservations[0]: r.status for r in rows} == {
        "1000000000/0001": RowStatus.FULLY_ISSUED,
        "1000000000/0002": RowStatus.PARTIALLY_ISSUED,
        "1000000000/0003": RowStatus.FULLY_ISSUED,
        "1000000000/0004": RowStatus.NOT_ISSUED,
        "1000000000/0005": RowStatus.NOT_ISSUED,
    }
    assert all(r.source is RowSource.SAP and r.session_id is None for r in rows)


def test_there_are_four_statuses() -> None:
    assert {s.value for s in RowStatus} == {"NO_RESERVATION", "NOT_ISSUED", "PARTIALLY_ISSUED", "FULLY_ISSUED"}


def test_variance_is_measured_against_the_reserved_quantity() -> None:
    (row,) = _rows([], [_sap("0001", reserved="100", issued="200")])
    assert (row.planned_quantity, row.issued_quantity) == (Decimal("100"), Decimal("200"))
    assert (row.variance_quantity, row.variance_percentage) == (Decimal("100"), Decimal("100"))


def test_nothing_issued_is_not_issued_whatever_the_date() -> None:
    for required in (PAST, AHEAD, None):
        (row,) = _rows([], [_sap("0001", reserved="10", issued="0", required=required)])
        assert row.status is RowStatus.NOT_ISSUED
        assert (row.variance_quantity, row.variance_percentage) == (Decimal("-10"), Decimal("-100"))


def test_a_partly_issued_reservation_is_partially_issued() -> None:
    (row,) = _rows([], [_sap("0001", issued="4", required=AHEAD)])
    assert row.status is RowStatus.PARTIALLY_ISSUED


def test_issued_with_nothing_reserved_is_fully_issued_with_no_percentage() -> None:
    (row,) = _rows([], [_sap("0001", reserved="0", issued="3")])
    assert row.status is RowStatus.FULLY_ISSUED
    assert (row.variance_quantity, row.variance_percentage) == (Decimal("3"), None)


def test_a_reservation_split_over_po_lines_is_one_row() -> None:
    rows = _rows([], [
        _sap("0001", issued="6", ledger_id="A", po_number="45", po_item="10"),
        _sap("0001", issued="6", ledger_id="B", po_number="45", po_item="20"),
    ])
    assert len(rows) == 1 and rows[0].issued_quantity == Decimal("6")


def test_non_oar_and_deleted_unissued_items_are_left_out() -> None:
    rows = _rows([], [
        _sap("0001", material_scope=MaterialScope.MIN_MAX),
        _sap("0002", reservation_deleted=True),
        _sap("0003", issued="2", reservation_deleted=True),
    ])
    assert [r.reservations for r in rows] == [("1000000000/0003",)]


def test_nothing_reserved_and_nothing_issued_is_no_row() -> None:
    assert _rows([], [_sap("0001", reserved="0")]) == []


# --- captured plans ------------------------------------------------------------


def test_a_reservation_a_plan_owns_is_on_the_plan_row_only() -> None:
    plan = _captured("P1", planned_quantity=Decimal("10"), linked_reservations=_linked("0001"))
    rows = _rows([plan], [_entry("0001", "10"), _sap("0002", issued="3")])
    plan_row = next(r for r in rows if r.source is RowSource.ASSISTANT)
    assert plan_row.reservations == ("1000000000/0001",)
    assert plan_row.status is RowStatus.FULLY_ISSUED and plan_row.session_id == "S-P1"
    assert [r.reservations for r in rows if r.source is RowSource.SAP] == [("1000000000/0002",)]


def test_a_reservation_in_an_unlinked_plans_window_stays_a_sap_row() -> None:
    """Same material, plant and window, but no item text carries the plan's
    ID: someone else's reservation. The plan awaits its own; the reservation
    keeps its own row."""
    rows = _rows([_captured("P1")], [_entry("0001", "10")])
    plan_row = next(r for r in rows if r.source is RowSource.ASSISTANT)
    assert plan_row.status is RowStatus.NO_RESERVATION and plan_row.reservations == ()
    assert [r.reservations for r in rows if r.source is RowSource.SAP] == [("1000000000/0001",)]


def test_plan_rows_read_partially_and_fully_issued() -> None:
    below = _rows([_captured("P1", linked_reservations=_linked("0001"))], [_entry("0001", "4")])
    above = _rows([_captured("P1", linked_reservations=_linked("0001"))], [_entry("0001", "12")])
    assert _status(below, "PLAN-P1") is RowStatus.PARTIALLY_ISSUED
    assert _status(above, "PLAN-P1") is RowStatus.FULLY_ISSUED
    assert next(r for r in above if r.row_id == "PLAN-P1").variance_quantity == Decimal("2")


def test_a_plan_with_no_reservation_yet_is_awaiting_one() -> None:
    (row,) = _rows([_captured("P1")], [])
    assert row.status is RowStatus.NO_RESERVATION
    assert row.reservations == () and row.issued_quantity == 0
    assert row.variance_quantity is None
    assert row.due_date == date(2026, 10, 31)


def test_awaiting_a_reservation_wins_even_when_the_window_is_ahead() -> None:
    (row,) = _rows([_captured("P1", window_end=AHEAD)], [])
    assert row.status is RowStatus.NO_RESERVATION


def test_a_linked_plan_with_nothing_issued_and_its_window_ahead_is_not_issued_nor_overdue() -> None:
    plan = _captured(
        "P1", window_end=AHEAD, reservation_number="1000000000", reservation_item="0001",
        linked_reservations=(("1000000000", "0001"),),
    )
    (row,) = _rows([plan], [_entry("0001", "0", requirement_date=AHEAD)])
    assert row.status is RowStatus.NOT_ISSUED and not is_overdue(row, TODAY)


def test_reference_plans_are_not_rows_and_own_nothing() -> None:
    reference = _captured(
        "REF", source=PlanSource.REFERENCE_CSV, reservation_number="1000000000", reservation_item="0001",
    )
    rows = _rows([reference], [_entry("0001", "10")])
    assert [r.source for r in rows] == [RowSource.SAP]


# --- stored form and the route's helpers ------------------------------------


def test_payload_round_trips() -> None:
    rows = _rows([_captured("P1"), _captured("P2", material="MAT2")], [_entry("0001", "4"), _sap("0002", issued="1")])
    assert [from_payload(to_payload(r)) for r in rows] == rows


def test_the_stored_record_carries_its_filter_columns() -> None:
    (row,) = _rows([_captured("P1", linked_reservations=_linked("0007"))], [_entry("0007", "4")])
    record = kinds.planvs(row)
    assert (record.kind, record.f1, record.f2, record.f3) == ("planvs", "ASSISTANT", "PARTIALLY_ISSUED", "1000000000")
    assert (record.rec_key, record.d1, record.text1) == ("PLAN-P1", date(2026, 10, 31), "S-P1")


def test_the_route_filters_by_source_status_and_reservation() -> None:
    rows = _rows(
        [_captured("P1", linked_reservations=_linked("0001"))],
        [_entry("0001", "4"), _sap("0002", issued="10", reservation_number="2000")],
    )
    sap = next(r for r in rows if r.source is RowSource.SAP)
    assert _matches(sap, source="sap", status="fully_issued", reservation="2000")
    assert not _matches(sap, source="ASSISTANT", status=None, reservation=None)
    assert not _matches(sap, source=None, status=None, reservation="1000000000")


def test_a_sap_row_shows_the_session_its_item_text_names() -> None:
    (row,) = _rows([], [_sap("0001", issued="1")])
    assert _with_session(row, {("1000000000", "0001"): "SABC"}).session_id == "SABC"
    assert _with_session(row, {}).session_id is None


# --- overdue: Planned Consumption Overdue, a captured plan's only -----------------


def _linked_plan(issued: str, **overrides):
    plan = _captured(
        "P1", reservation_number="1000000000", reservation_item="0001",
        linked_reservations=(("1000000000", "0001"),), **overrides,
    )
    (row,) = _rows([plan], [_entry("0001", issued)])
    return row


def test_a_linked_plan_whose_window_ended_with_nothing_issued_is_overdue() -> None:
    row = _linked_plan("0")
    assert row.status is RowStatus.NOT_ISSUED
    assert is_overdue(row, TODAY) and not is_overdue(row, date(2026, 10, 31))


def test_a_partly_issued_plan_is_not_overdue() -> None:
    """Same rule as the PLAN_BREACH exception: any issue clears it."""
    assert not is_overdue(_linked_plan("4"), TODAY)


def test_a_sap_reservation_is_never_overdue() -> None:
    """No consumption plan, nothing to be overdue against."""
    rows = _rows([], [_sap("0001", issued="0", required=PAST), _sap("0002", issued="4", required=PAST)])
    assert not any(is_overdue(r, TODAY) for r in rows)


def test_awaiting_a_reservation_is_never_overdue() -> None:
    awaiting = _rows([_captured("P1")], [])[0]
    assert not is_overdue(awaiting, TODAY)


def test_the_route_marks_and_filters_overdue_rows() -> None:
    due = _linked_plan("0")
    sap = _rows([], [_sap("0001", required=PAST)])[0]
    assert _response(due, {}, TODAY).overdue and not _response(sap, {}, TODAY).overdue
    assert _matches(due, source=None, status=None, reservation=None, overdue=True, as_of=TODAY)
    assert not _matches(sap, source=None, status=None, reservation=None, overdue=True, as_of=TODAY)


def test_a_version_stored_before_layout_8_reads_with_the_new_statuses() -> None:
    row = _linked_plan("4")
    for legacy, issued, expected in (
        ("BELOW_PLAN", "4", RowStatus.PARTIALLY_ISSUED),
        ("BELOW_PLAN", "0", RowStatus.NOT_ISSUED),
        ("NOT_DUE", "0", RowStatus.NOT_ISSUED),
        ("ON_PLAN", "10", RowStatus.FULLY_ISSUED),
        ("ABOVE_PLAN", "12", RowStatus.FULLY_ISSUED),
    ):
        payload = {**to_payload(row), "st": legacy, "iq": issued}
        assert from_payload(payload).status is expected

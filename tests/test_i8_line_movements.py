"""The goods movements on a repair PO line (4-Nov-2026), without a database.

Two rules, both ruled 4-Nov-2026:

* a 542 reverses a 541, so a dispatch SAP reversed does not put a line at the
  vendor;
* a line received in full (101 less 102, less 122 plus 123) is not out for
  repair, even while SAP has not ticked "Delivery completed" on it. It is
  left out of the register and counted; a line received in part keeps only
  the units still out.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.initiatives.i8 import register
from app.initiatives.i8.config import I8Settings
from app.initiatives.i8.register import (
    AT_VENDOR,
    PO_ISSUED,
    _build_line,
    is_fully_received,
    load_repair_lines,
    movement_params,
)

CFG = I8Settings(_env_file=None)
TODAY = date(2026, 11, 4)


def candidate(ebelp="10", menge="1", **overrides) -> dict:
    """An open repair PO line as the EKPO pull returns it."""
    row = dict(
        ebeln="4500001696", ebelp=ebelp, matnr="8000004055", werks="1500", pstyp="3",
        txz01="PUMP REPAIR", menge=Decimal(menge), meins="EA", netpr=Decimal("1000"),
        elikz=None, banfn=None, bnfpo=None, erdat=None, loekz=None, afnam=None,
    )
    row.update(overrides)
    return row


def movements(first_dispatch=None, qty="0", received="0", postings=0) -> dict:
    """One line's row from the movements query."""
    return dict(
        ebeln="4500001696", ebelp="10", first_dispatch=first_dispatch, last_dispatch=first_dispatch,
        qty=Decimal(qty), postings=postings, received=Decimal(received),
    )


def build(row, dispatch=None):
    return _build_line(
        row, schedule=None, dispatch=dispatch, header=None, vendor_names={}, lead_time=None, today=TODAY, cfg=CFG
    )


class TestTheDispatch:
    def test_a_541_puts_the_line_at_the_vendor(self) -> None:
        line = build(candidate(), movements(date(2026, 7, 24), qty="1", postings=1))
        assert line.repair_status == AT_VENDOR
        assert line.dispatched_at == date(2026, 7, 24)
        assert line.dispatched_qty == 1

    def test_a_541_reversed_by_a_542_does_not(self) -> None:
        # The query nets the 542 off: one 541 and one 542 leave 0.
        line = build(candidate(), movements(date(2026, 7, 24), qty="0", postings=1))
        assert line.repair_status == PO_ISSUED
        assert line.dispatched_at is None
        assert line.days_at_vendor is None

    def test_more_reversed_than_sent_is_not_negative(self) -> None:
        line = build(candidate(), movements(date(2026, 7, 24), qty="-1", postings=1))
        assert line.dispatched_qty == 0
        assert line.dispatched_at is None

    def test_the_movement_types_are_settings(self) -> None:
        cfg = I8Settings(_env_file=None, dispatch_reversal_movement_type="X42", receipt_movement_type="X01")
        params = movement_params(cfg)
        assert params["dispatch_reversal"] == "X42"
        assert params["receipt"] == "X01"
        assert movement_params(CFG) == {
            "dispatch": "541",
            "dispatch_reversal": "542",
            "vendor_stock": "O",
            "receipt": "101",
            "receipt_reversal": "102",
            "return_to_vendor": "122",
            "return_reversal": "123",
        }


class TestTheReceipts:
    def test_a_line_received_in_part_keeps_only_the_units_still_out(self) -> None:
        line = build(candidate(menge="2"), movements(received="1"))
        assert line.received_qty == 1
        assert line.qty_under_repair == 1
        assert line.ordered_qty == 2

    def test_nothing_received_leaves_the_whole_line_out(self) -> None:
        line = build(candidate(menge="2"), movements(received="0"))
        assert line.qty_under_repair == 2
        assert line.received_qty == 0

    def test_a_line_with_no_movements_at_all(self) -> None:
        line = build(candidate(menge="2"))
        assert line.qty_under_repair == 2
        assert line.repair_status == PO_ISSUED

    def test_fully_received(self) -> None:
        assert is_fully_received(Decimal(1), Decimal(1)) is True
        assert is_fully_received(Decimal(2), Decimal(3)) is True
        assert is_fully_received(Decimal(2), Decimal(1)) is False
        # Nothing ordered is never called received: nothing to measure against.
        assert is_fully_received(Decimal(0), Decimal(0)) is False


class TestTheRegisterLeavesOutWhatCameBack:
    def test_a_line_received_in_full_is_left_out_and_counted(self, monkeypatch) -> None:
        """The Weir pumps of 24-Jul-2026: sent, received, and still "At Vendor"
        because nobody ticked "Delivery completed"."""
        rows = [candidate("10"), candidate("20", menge="2"), candidate("30")]
        moves = {
            ("4500001696", "10"): movements(date(2026, 7, 24), qty="1", received="1", postings=1),
            ("4500001696", "20"): movements(date(2026, 7, 24), qty="2", received="1", postings=1),
        }

        def evidence(db, documents, materials, cfg, sources=None):
            return {}, moves, {}, {}, {}

        monkeypatch.setattr(register, "_evidence", evidence)
        lines, stats = load_repair_lines(None, CFG, today=TODAY, candidates=rows)

        assert [line.item for line in lines] == ["20", "30"]
        assert stats.excluded_received_lines == 1
        assert stats.total_lines == stats.open_lines == 2
        received_in_part = lines[0]
        assert received_in_part.qty_under_repair == 1
        assert received_in_part.repair_status == AT_VENDOR

    def test_a_line_sap_closed_is_counted_as_closed_not_received(self, monkeypatch) -> None:
        rows = [candidate("10", elikz="X")]
        moves = {("4500001696", "10"): movements(received="1")}
        monkeypatch.setattr(register, "_evidence", lambda *a, **k: ({}, moves, {}, {}, {}))
        lines, stats = load_repair_lines(None, CFG, today=TODAY, candidates=rows)
        assert lines == []
        assert stats.excluded_delivery_completed_lines == 1
        assert stats.excluded_received_lines == 0

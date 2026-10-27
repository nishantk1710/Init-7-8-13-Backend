"""FR-8 -- UNJUSTIFIED_ACQUISITION, checked on the new-buy PR, and the
deleted/blocked PO-line rule.

No database. The detector is a pure function of purchases, open repairs and
justifications, and every decision it makes is pinned here: which PR items and
PO lines are new purchases (since 27-Oct-2026 the new-buy PR, plus direct PO
lines with no PR behind it), what "a repair was open at the time" means (a
repair PO line or an open repair PR), which justifications answer a purchase,
how a pre-cutover purchase is labelled, and that the text a person reads names
no SAP table or field. Rows are hand-built in the ``v_eban`` / ``v_ekpo`` /
``v_ekko`` / ``v_resb`` column names.
"""

from __future__ import annotations

import ast
import pathlib
import re
from datetime import date
from decimal import Decimal

import pytest

from app.initiatives.i8 import staged
from app.initiatives.i8.acquisitions import (
    PURCHASE_ORDER,
    PURCHASE_REQUISITION,
    JustificationRecord,
    NewAcquisition,
    OpenRepair,
    find_new_acquisitions,
    is_direct_purchase_line,
    is_new_buy_requisition,
    is_new_purchase_line,
    load_new_acquisitions,
    open_repair_at,
    repair_order_references,
    repairs_by_material_plant,
)
from app.initiatives.i8.attestation import AttestationCoverage
from app.initiatives.i8.config import I8Settings
from app.initiatives.i8.exceptions import (
    RAISED_BY_I8,
    ExceptionType,
    build_exceptions,
    unjustified_acquisitions,
)
from app.initiatives.i8.register import RepairLine, is_deleted_line
from app.initiatives.i8.requisitions import RepairRequest

CFG = I8Settings(_env_file=None)
BOUGHT = date(2026, 5, 10)
NON_ORDER = frozenset({"WK", "MK", "AN", "FPA"})  # I13_NON_ORDER_DOC_TYPES' default


def repair(**overrides) -> RepairLine:
    """An open repair PO line of 8000005632 at 1300, raised before BOUGHT."""
    defaults = dict(
        purchasing_document="4500001234",
        item="10",
        material_id="8000005632",
        plant="1300",
        description="Pump repair",
        ordered_qty=Decimal(1),
        unit="EA",
        net_price=None,
        item_category="3",
        pr_number=None,
        pr_item=None,
        requisitioner=None,
        doc_type="ZREP",
        corroborated_by_doc_type=True,
        has_po_header=True,
        vendor="400093",
        vendor_name=None,
        raised_at=date(2026, 5, 1),
        po_date=date(2026, 5, 1),
        due_date=date(2026, 6, 1),
        schedule_lines=1,
        dispatched_at=None,
        dispatched_qty=Decimal(0),
        lead_time_days=None,
        repair_status="PO Issued",
        overdue_status="ON_TIME",
        lead_time_status="NO_LEAD_TIME",
        qty_under_repair=Decimal(1),
        days_open=9,
        days_elapsed=9,
        days_over_lead_time=None,
        days_at_vendor=None,
        days_in_current_stage=9,
        days_remaining=22,
        aging_bucket="0-15",
    )
    defaults.update(overrides)
    return RepairLine(**defaults)


def repair_request(**overrides) -> RepairRequest:
    """An open repair PR (no PO yet) of 8000005632 at 1300, requested 04 Feb."""
    defaults = dict(
        purchase_requisition="2000029113",
        item="10",
        material_id="8000005632",
        plant="1300",
        description="PUMP REPAIR",
        quantity=Decimal(1),
        unit="EA",
        requested_on=date(2026, 2, 4),
        need_date=None,
        department="10305",
        requisition_status="N",
        document_type="ZMNP",
        repair_order="940000004981",
        reservation="1916164",
        days_open=0,
        aging_bucket="0-15",
    )
    defaults.update(overrides)
    return RepairRequest(**defaults)


def purchase(**overrides) -> NewAcquisition:
    """A new-buy PR for 8000005632 at 1300, raised on BOUGHT."""
    defaults = dict(
        purchasing_document="1000201234",
        item="10",
        material_id="8000005632",
        plant="1300",
        description="PUMP, CENTRIFUGAL",
        ordered_qty=Decimal(1),
        raised_at=BOUGHT,
        document_type=PURCHASE_REQUISITION,
        requested_by="REP",
    )
    defaults.update(overrides)
    return NewAcquisition(**defaults)


def po_purchase(**overrides) -> NewAcquisition:
    """A direct PO line for the same part: what every purchase was before."""
    defaults = dict(
        purchasing_document="4100100001",
        item="10",
        material_id="8000005632",
        plant="1300",
        description="PUMP, CENTRIFUGAL",
        ordered_qty=Decimal(1),
        raised_at=BOUGHT,
    )
    defaults.update(overrides)
    return NewAcquisition(**defaults)


def eban_row(banfn="1000201234", bnfpo="10", **overrides) -> dict:
    row = dict(
        banfn=banfn, bnfpo=bnfpo, matnr="8000005632", werks="1300", txz01="PUMP, CENTRIFUGAL",
        menge=Decimal(1), badat=BOUGHT, loekz=None, afnam="REP", rsnum=None,
    )
    row.update(overrides)
    return row


def ekpo_row(**overrides) -> dict:
    row = dict(
        ebeln="4100100001", ebelp="10", matnr="8000005632", werks="1300",
        pstyp="0", txz01="PUMP", menge=Decimal(1), erdat=None, loekz=None,
        banfn=None, bnfpo=None,
    )
    row.update(overrides)
    return row


def ekko_row(ebeln="4100100001", bsart="NB", aedat=BOUGHT) -> dict:
    return dict(ebeln=ebeln, bsart=bsart, lifnr="400093", aedat=aedat)


def resb_row(**overrides) -> dict:
    """A repair order's line for the faulty unit, naming its repair PR."""
    row = dict(
        rsnum="1916164", rspos="1", matnr="8000005632", werks="1300", bwart="543",
        banfn="2000029113", bnfpo="10", aufnr="940000004981", xloek=None, kzear=None,
    )
    row.update(overrides)
    return row


def find(requisitions=(), candidates=(), headers=None, reservation_lines=(), cfg=CFG):
    return find_new_acquisitions(
        requisitions,
        candidates,
        {h["ebeln"]: h for h in (headers if headers is not None else [ekko_row()])},
        reservation_lines,
        cfg,
        non_order_doc_types=NON_ORDER,
    )


def raised(acquisitions, lines, justifications=(), *, window=30, cutover=None, requests=()):
    return unjustified_acquisitions(
        acquisitions,
        lines,
        justifications,
        window_days=window,
        cutover=cutover,
        repair_requests=requests,
    )


# --- which PR items are new-buy purchases ---------------------------------------


class TestWhichRequisitionsAreNewBuys:
    """An 80-series PR item, not deleted, that no repair order names and that
    is not raised from a repair order's reservation."""

    def refs(self, *lines):
        return repair_order_references(lines, CFG)

    def test_a_plain_stock_pr_on_an_eighty_series_part(self) -> None:
        assert is_new_buy_requisition(eban_row(), *self.refs(), CFG) is True

    def test_a_part_that_is_not_repairable_is_out_of_scope(self) -> None:
        assert is_new_buy_requisition(eban_row(matnr="1000000123"), *self.refs(), CFG) is False

    def test_a_deleted_pr_bought_nothing(self) -> None:
        """EBAN marks a deleted item 'X', not EKPO's 'L'."""
        assert is_new_buy_requisition(eban_row(loekz="X"), *self.refs(), CFG) is False
        assert is_new_buy_requisition(eban_row(loekz="L"), *self.refs(), CFG) is True

    def test_a_pr_a_repair_order_names_is_a_repair_not_a_purchase(self) -> None:
        refs = self.refs(resb_row(banfn="1000201234", bnfpo="10"))
        assert is_new_buy_requisition(eban_row(), *refs, CFG) is False
        # Another item of the same PR is not named, so it is still a purchase.
        assert is_new_buy_requisition(eban_row(bnfpo="20"), *refs, CFG) is True

    def test_a_closed_repair_order_line_still_makes_its_pr_a_repair(self) -> None:
        refs = self.refs(resb_row(banfn="1000201234", bnfpo="10", xloek="X", kzear="X"))
        assert is_new_buy_requisition(eban_row(), *refs, CFG) is False

    def test_a_pr_raised_from_a_repair_orders_reservation_is_a_repair(self) -> None:
        """Even when the repair order's line names no PR."""
        refs = self.refs(resb_row(banfn=None, bnfpo=None, rsnum="1916164"))
        assert is_new_buy_requisition(eban_row(rsnum="1916164"), *refs, CFG) is False
        assert is_new_buy_requisition(eban_row(rsnum="1916999"), *refs, CFG) is True

    def test_only_repair_order_lines_count(self) -> None:
        """A maintenance order's line (261) names no repair."""
        refs = self.refs(resb_row(bwart="261", banfn="1000201234", bnfpo="10", rsnum="1916164"))
        assert is_new_buy_requisition(eban_row(rsnum="1916164"), *refs, CFG) is True

    def test_the_movement_type_and_the_deletion_flag_are_settings(self) -> None:
        cfg = I8Settings(
            _env_file=None, repair_reservation_movement_type="999", requisition_deleted_indicator="D"
        )
        refs = repair_order_references([resb_row(bwart="999", banfn="1000201234")], cfg)
        assert is_new_buy_requisition(eban_row(), *refs, cfg) is False
        assert is_new_buy_requisition(eban_row(banfn="1", loekz="X"), *refs, cfg) is True
        assert is_new_buy_requisition(eban_row(banfn="1", loekz="D"), *refs, cfg) is False

    def test_a_pr_becomes_a_new_acquisition_dated_by_its_pr_date(self) -> None:
        [found] = find(requisitions=[eban_row()])
        assert found.document_type == PURCHASE_REQUISITION
        assert found.key == (PURCHASE_REQUISITION, "1000201234", "10")
        assert found.raised_at == BOUGHT
        assert found.requested_by == "REP"
        assert found.reference == "PR-1000201234-10"

    def test_a_pr_with_no_quantity_reads_zero_not_a_guess(self) -> None:
        [found] = find(requisitions=[eban_row(menge=None)])
        assert found.ordered_qty == 0


# --- which PO lines are direct purchases ------------------------------------------


class TestWhatCountsAsANewPurchaseLine:
    def test_a_standard_line_on_an_eighty_series_part(self) -> None:
        assert is_new_purchase_line(ekpo_row(), CFG) is True

    def test_a_repair_line_is_not_a_purchase(self) -> None:
        assert is_new_purchase_line(ekpo_row(pstyp="3"), CFG) is False

    def test_a_part_that_is_not_repairable_is_out_of_scope(self) -> None:
        assert is_new_purchase_line(ekpo_row(matnr="1000000123"), CFG) is False

    def test_a_deleted_purchase_bought_nothing(self) -> None:
        assert is_new_purchase_line(ekpo_row(loekz="L"), CFG) is False

    def test_a_blocked_purchase_is_still_a_purchase(self) -> None:
        assert is_new_purchase_line(ekpo_row(loekz="S"), CFG) is True

    def test_the_item_category_follows_configuration(self) -> None:
        cfg = I8Settings(_env_file=None, new_purchase_item_category="NB")
        assert is_new_purchase_line(ekpo_row(pstyp="NB"), cfg) is True
        assert is_new_purchase_line(ekpo_row(pstyp="0"), cfg) is False


class TestDirectPurchaseOrders:
    """A PO line raised with no PR behind it, for a quantity, on an order."""

    def direct(self, row=None, header=...) -> bool:
        return is_direct_purchase_line(
            row or ekpo_row(), ekko_row() if header is ... else header, CFG, NON_ORDER
        )

    def test_a_standard_line_with_no_pr_on_an_order(self) -> None:
        assert self.direct() is True

    def test_a_line_raised_from_a_pr_is_the_prs_purchase(self) -> None:
        assert self.direct(ekpo_row(banfn="1000201234", bnfpo="10")) is False

    @pytest.mark.parametrize("menge", [Decimal(0), None])
    def test_a_line_for_nothing_bought_nothing(self, menge) -> None:
        assert self.direct(ekpo_row(menge=menge)) is False

    @pytest.mark.parametrize("bsart", ["WK", "MK", "AN", "FPA", "an"])
    def test_contracts_and_rfqs_are_not_orders(self, bsart) -> None:
        """I13's shared rule, read rather than copied: case as SAP sends it."""
        assert self.direct(header=ekko_row(bsart=bsart)) is False

    def test_a_line_with_no_header_is_an_order(self) -> None:
        assert self.direct(header=None) is True

    def test_a_deleted_or_repair_line_is_not_a_direct_purchase(self) -> None:
        assert self.direct(ekpo_row(loekz="L")) is False
        assert self.direct(ekpo_row(pstyp="3")) is False

    def test_dated_by_the_po_headers_created_on_date(self) -> None:
        """Not EKPO.ERDAT: the live data does not carry it."""
        [found] = find(candidates=[ekpo_row(erdat=date(2020, 1, 1))], headers=[ekko_row(aedat=BOUGHT)])
        assert found.document_type == PURCHASE_ORDER
        assert found.raised_at == BOUGHT
        assert found.requested_by is None
        assert found.reference == "4100100001-10"

    def test_no_header_means_no_date(self) -> None:
        [found] = find(candidates=[ekpo_row()], headers=[])
        assert found.raised_at is None


class TestOnePurchaseCountedOnce:
    def test_a_pr_that_became_a_po_is_counted_on_the_pr_only(self) -> None:
        found = find(
            requisitions=[eban_row()],
            candidates=[ekpo_row(banfn="1000201234", bnfpo="10")],
        )
        assert [a.key for a in found] == [(PURCHASE_REQUISITION, "1000201234", "10")]

    def test_prs_and_direct_pos_together_in_key_order(self) -> None:
        found = find(
            requisitions=[eban_row(), eban_row(banfn="2000029113")],
            candidates=[ekpo_row(), ekpo_row(ebelp="20", pstyp="3")],
            reservation_lines=[resb_row()],  # names 2000029113/10: a repair
        )
        assert [a.key for a in found] == [
            (PURCHASE_ORDER, "4100100001", "10"),
            (PURCHASE_REQUISITION, "1000201234", "10"),
        ]

    def test_a_pr_and_a_po_with_the_same_number_are_different_purchases(self) -> None:
        found = find(requisitions=[eban_row(banfn="4100100001")], candidates=[ekpo_row()])
        assert len({a.key for a in found}) == 2


# --- the loader, over a fake session ----------------------------------------------


class _Result:
    def __init__(self, rows) -> None:
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class _Db:
    def __init__(self, eban_rows, headers) -> None:
        self.tables = {"eban": eban_rows, "ekko": headers}
        self.calls: list[tuple[str, dict]] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        for name, rows in self.tables.items():
            if f"_{name}" in sql:
                return _Result(rows)
        raise AssertionError(f"unexpected query: {sql}")


class TestTheLoader:
    def test_it_reads_the_prs_and_only_the_direct_lines_headers(self) -> None:
        db = _Db([eban_row(), eban_row(banfn="2000029113")], [ekko_row()])
        candidates = [
            ekpo_row(),
            ekpo_row(ebeln="4100100002", banfn="1000201234", bnfpo="10"),  # the PR's PO
            ekpo_row(ebeln="4100100003", pstyp="3"),  # a repair line
        ]
        found = load_new_acquisitions(
            db, CFG, candidates=candidates, reservation_lines=[resb_row()], non_order_doc_types=NON_ORDER
        )
        assert [a.key for a in found] == [
            (PURCHASE_ORDER, "4100100001", "10"),
            (PURCHASE_REQUISITION, "1000201234", "10"),
        ]
        sql, params = db.calls[0]
        assert "from v_eban" in sql and "like" in sql
        # The series prefix is a bound pattern, never a literal in the query.
        assert set(params.values()) == {f"{p}%" for p in CFG.series_prefix_list}
        assert "from v_ekko" in db.calls[1][0] and db.calls[1][1]["documents"] == ["4100100001"]
        assert len(db.calls) == 2

    def test_it_reads_the_staged_copies(self) -> None:
        db = _Db([eban_row()], [])
        sources = staged.Sources(views={"v_eban": "#i8s_v_eban", "v_ekko": "#i8s_v_ekko"})
        found = load_new_acquisitions(
            db, CFG, candidates=[], reservation_lines=[], sources=sources, non_order_doc_types=NON_ORDER
        )
        assert len(found) == 1
        assert "from #i8s_v_eban" in db.calls[0][0]
        assert len(db.calls) == 1  # no direct lines: no headers read

    def test_the_non_order_types_default_to_i13s_setting(self, monkeypatch) -> None:
        from app.core import config as core_config

        class _Settings:
            i13_non_order_doc_type_set = frozenset({"ZZ"})

        monkeypatch.setattr(core_config, "get_settings", lambda: _Settings())
        db = _Db([], [ekko_row(bsart="ZZ")])
        assert load_new_acquisitions(db, CFG, candidates=[ekpo_row()], reservation_lines=[]) == []


# --- the deleted / blocked PO-line rule --------------------------------------------


class TestDeletedAndBlockedLines:
    def test_the_deleted_value_is_read_from_configuration(self) -> None:
        assert is_deleted_line({"loekz": "L"}, CFG) is True
        assert is_deleted_line({"loekz": "S"}, CFG) is False
        assert is_deleted_line({"loekz": None}, CFG) is False
        cfg = I8Settings(_env_file=None, po_deleted_indicator="X")
        assert is_deleted_line({"loekz": "X"}, cfg) is True

    def test_no_loekz_or_item_category_literal_in_the_logic(self) -> None:
        """Same discipline as the repair convention: the values are settings."""
        for name in ("register.py", "acquisitions.py", "requisitions.py"):
            tree = ast.parse(
                (pathlib.Path("app/initiatives/i8") / name).read_text(encoding="utf-8")
            )
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                literals = {
                    n.value
                    for n in [node.left, *node.comparators]
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                }
                assert not literals & {"L", "S", "0", "X", "3", "543", "AN", "ZREP"}, (
                    f"{name}: {literals} compared literally"
                )


# --- was a repair open when the purchase was raised ---------------------------------


class TestOpenRepairAtThePurchase:
    """Every repair here is open in SAP, so one raised by the day of the
    purchase has been out ever since. One SAP has closed is not there to match."""

    def index(self, *lines, requests=()):
        return repairs_by_material_plant(lines, requests)

    def test_an_open_repair_raised_before_the_purchase(self) -> None:
        line = repair()
        assert open_repair_at(purchase(), self.index(line)) == OpenRepair.from_line(line)

    def test_a_repair_raised_the_same_day_counts(self) -> None:
        line = repair(raised_at=BOUGHT)
        assert open_repair_at(purchase(), self.index(line)) == OpenRepair.from_line(line)

    def test_a_repair_raised_after_the_purchase_does_not(self) -> None:
        assert open_repair_at(purchase(), self.index(repair(raised_at=date(2026, 5, 11)))) is None

    def test_an_undated_repair_cannot_be_placed_in_time(self) -> None:
        assert open_repair_at(purchase(), self.index(repair(raised_at=None))) is None

    def test_another_plant_is_another_shelf(self) -> None:
        assert open_repair_at(purchase(), self.index(repair(plant="1500"))) is None

    def test_the_soonest_due_repair_is_the_one_reported(self) -> None:
        late = repair(item="10", due_date=date(2026, 7, 1))
        soon = repair(item="20", due_date=date(2026, 5, 20))
        undated = repair(item="30", due_date=None)
        assert open_repair_at(purchase(), self.index(late, undated, soon)).item == "20"

    def test_an_undated_purchase_cannot_be_placed_in_time(self) -> None:
        assert open_repair_at(purchase(raised_at=None), self.index(repair())) is None

    def test_an_open_repair_pr_counts_from_its_pr_date(self) -> None:
        found = open_repair_at(purchase(), self.index(requests=[repair_request()]))
        assert found is not None
        assert found.key == (PURCHASE_REQUISITION, "2000029113", "10")
        assert found.raised_at == date(2026, 2, 4)
        assert found.due_date is None
        assert found.reference == "PR-2000029113-10"

    def test_a_repair_pr_requested_after_the_purchase_does_not(self) -> None:
        later = repair_request(requested_on=date(2026, 5, 11))
        assert open_repair_at(purchase(), self.index(requests=[later])) is None

    def test_a_repair_pr_sorts_after_po_repairs_even_undated_ones(self) -> None:
        """A PR has no due date; among the undated, a PO comes first."""
        dated = repair(item="10", due_date=date(2026, 7, 1))
        undated = repair(item="20", due_date=None)
        request = repair_request()
        assert open_repair_at(purchase(), self.index(dated, requests=[request])).item == "10"
        assert open_repair_at(purchase(), self.index(undated, requests=[request])).key == (
            PURCHASE_ORDER,
            "4500001234",
            "20",
        )
        assert open_repair_at(purchase(), self.index(requests=[request])).document_type == (
            PURCHASE_REQUISITION
        )


# --- the exception -----------------------------------------------------------------


_TABLE_VOCABULARY = re.compile(
    r"\b(EBAN|EKPO|EKKO|RESB|BANFN|BNFPO|EBELN|EBELP|BADAT|AEDAT|ERDAT|AFNAM|NEW_ACQUISITION|"
    r"justification|SAP)\b"
)


class TestUnjustifiedAcquisition:
    def test_a_new_buy_pr_while_a_repair_pr_was_open(self) -> None:
        [item] = raised([purchase(raised_at=date(2026, 3, 14))], [], requests=[repair_request()])
        assert item.type == ExceptionType.UNJUSTIFIED_ACQUISITION.value
        assert item.id == "EX-UNJUSTIFIED_ACQUISITION-PR-1000201234-10"
        assert (item.acquisition_document, item.acquisition_item) == ("1000201234", "10")
        assert item.acquisition_document_type == PURCHASE_REQUISITION
        # The repair: the bare PR number, typed PR, named as the register names its row.
        assert (item.purchasing_document, item.item) == ("2000029113", "10")
        assert item.repair_document_type == PURCHASE_REQUISITION
        assert item.repair_reference == "PR-2000029113-10"
        assert item.severity == "warning"
        assert item.raised_at == date(2026, 3, 14)
        assert item.detail == (
            "A new unit was requested on 14 Mar 2026 (PR 1000201234/10) while a repair "
            "of the same part (repair PR 2000029113/10, requested 04 Feb 2026) was still "
            "open, and no reason was recorded within 30 days."
        )

    def test_a_new_buy_pr_while_a_repair_po_was_open(self) -> None:
        [item] = raised([purchase()], [repair()])
        assert item.id == "EX-UNJUSTIFIED_ACQUISITION-PR-1000201234-10"
        assert (item.purchasing_document, item.item) == ("4500001234", "10")
        assert item.repair_document_type == PURCHASE_ORDER
        assert item.repair_reference == "4500001234-10"
        assert "(repair PO 4500001234/10, raised 01 May 2026)" in item.detail

    def test_a_direct_po_keeps_the_id_it_always_had(self) -> None:
        [item] = raised([po_purchase()], [repair()])
        assert item.id == "EX-UNJUSTIFIED_ACQUISITION-4100100001-10"
        assert item.acquisition_document_type == PURCHASE_ORDER
        assert item.detail.startswith("A new unit was ordered on 10 May 2026 (PO 4100100001/10)")

    def test_the_text_names_no_table_field_or_rule(self) -> None:
        for item in [
            *raised([purchase()], [repair()]),
            *raised([po_purchase()], [], requests=[repair_request()]),
            *raised([purchase()], [repair()], cutover=date(2026, 9, 1)),
        ]:
            assert not _TABLE_VOCABULARY.search(item.detail), item.detail
            assert not _TABLE_VOCABULARY.search(item.title), item.title
            assert not re.search(r"\d{4}-\d{2}-\d{2}", item.detail), "dates read as 08 May 2025"

    def test_open_repair_prs_count_whatever_the_register_serves(self) -> None:
        """The check is handed every open repair PR; no switch is read here."""
        assert len(raised([purchase()], [], requests=[repair_request()])) == 1

    def test_not_raised_when_no_repair_was_open(self) -> None:
        """Buying a part nobody is repairing is ordinary procurement."""
        assert raised([purchase()], []) == []
        assert raised([purchase()], [repair(raised_at=date(2026, 5, 11))]) == []
        assert raised([purchase()], [], requests=[repair_request(requested_on=date(2026, 5, 11))]) == []

    def test_an_undated_purchase_is_never_raised(self) -> None:
        assert raised([po_purchase(raised_at=None)], [repair()]) == []

    @pytest.mark.parametrize("recorded", [date(2026, 4, 10), BOUGHT, date(2026, 6, 9)])
    def test_a_justification_inside_the_window_answers_it(self, recorded) -> None:
        reason = JustificationRecord(material_id="8000005632", plant="1300", recorded_on=recorded)
        assert raised([purchase()], [repair()], [reason]) == []
        assert raised([purchase()], [], [reason], requests=[repair_request()]) == []

    def test_a_justification_outside_the_window_does_not(self) -> None:
        reason = JustificationRecord(
            material_id="8000005632", plant="1300", recorded_on=date(2026, 6, 10)
        )
        assert len(raised([purchase()], [repair()], [reason])) == 1

    def test_a_justification_for_another_plant_does_not(self) -> None:
        reason = JustificationRecord(material_id="8000005632", plant="1500", recorded_on=BOUGHT)
        assert len(raised([purchase()], [repair()], [reason])) == 1

    def test_a_justification_against_the_exception_id_answers_it_whenever_recorded(self) -> None:
        reason = JustificationRecord(
            material_id="8000005632",
            plant="1300",
            recorded_on=date(2026, 12, 1),
            exception_id="EX-UNJUSTIFIED_ACQUISITION-PR-1000201234-10",
        )
        assert raised([purchase()], [repair()], [reason]) == []

    def test_the_window_is_configuration(self) -> None:
        reason = JustificationRecord(
            material_id="8000005632", plant="1300", recorded_on=date(2026, 5, 20)
        )
        assert raised([purchase()], [repair()], [reason], window=10) == []
        assert len(raised([purchase()], [repair()], [reason], window=5)) == 1

    def test_before_the_cutover_it_is_labelled_not_accused(self) -> None:
        [item] = raised([purchase()], [repair()], cutover=date(2026, 9, 1))
        assert item.pre_automation is True
        assert item.severity == "info"
        assert item.title == "Raised before Spares Automation"
        assert item.detail.startswith(
            "A new unit was requested on 10 May 2026 (PR 1000201234/10), before reasons "
            "for buying new were first asked for on 01 Sep 2026."
        )

    def test_after_the_cutover_it_is_a_warning(self) -> None:
        [item] = raised([purchase()], [repair()], cutover=date(2026, 1, 1))
        assert item.pre_automation is False
        assert item.severity == "warning"
        assert item.title == "New unit bought while a repair was open"

    def test_it_is_now_a_raised_type(self) -> None:
        assert ExceptionType.UNJUSTIFIED_ACQUISITION in RAISED_BY_I8
        # Raised too since 27-Oct-2026 (job lines with no R-reference).
        assert ExceptionType.MISSING_SESSION_ID in RAISED_BY_I8

    def test_other_checks_name_po_lines(self) -> None:
        line = repair()
        coverage = AttestationCoverage(covered={}, uncovered={line.key}, window_days=30)
        items, _ = build_exceptions([line], coverage)
        [item] = items
        assert item.repair_document_type == PURCHASE_ORDER
        assert item.repair_reference == "4500001234-10"

    def test_both_detectors_share_one_queue_and_one_set_of_counts(self) -> None:
        line = repair()
        coverage = AttestationCoverage(covered={}, uncovered={line.key}, window_days=30)
        items, stats = build_exceptions(
            [line],
            coverage,
            acquisitions=[
                purchase(),
                purchase(item="20", raised_at=date(2026, 4, 1)),
                po_purchase(material_id="8000000001"),
            ],
            justification_window_days=30,
            justification_cutover=date(2026, 5, 5),
            repair_requests=[repair_request(material_id="8000000001")],
        )
        assert stats.by_type == {"MISSING_ATTESTATION": 1, "UNJUSTIFIED_ACQUISITION": 2}
        assert stats.acquisitions_checked == 3
        assert stats.total == 3
        assert stats.pre_automation == 0
        assert stats.actionable == 3
        assert stats.justification_cutover_date == date(2026, 5, 5)
        bought_new = sorted(i.id for i in items if i.type == "UNJUSTIFIED_ACQUISITION")
        assert bought_new == [
            "EX-UNJUSTIFIED_ACQUISITION-4100100001-10",
            "EX-UNJUSTIFIED_ACQUISITION-PR-1000201234-10",
        ]


class TestConfigurationDefaults:
    def test_new_settings_have_working_defaults(self) -> None:
        cfg = I8Settings(_env_file=None)
        assert cfg.po_deleted_indicator == "L"
        assert cfg.po_blocked_indicator == "S"
        assert cfg.delivery_completed_indicator == "X"
        assert cfg.new_purchase_item_category == "0"
        assert cfg.justification_window_days == 30
        assert cfg.justification_cutover_date_value is None
        assert cfg.attestation_max_quantity == 1000

    def test_a_malformed_justification_cutover_fails_loudly(self) -> None:
        with pytest.raises(ValueError):
            I8Settings(_env_file=None, justification_cutover_date="01-10-2026").justification_cutover_date_value

    def test_the_cutover_is_documented_as_the_release_date(self) -> None:
        text = pathlib.Path(".env.example").read_text(encoding="utf-8")
        block = text[text.index("I8_JUSTIFICATION_WINDOW_DAYS") : text.index("I8_JUSTIFICATION_CUTOVER_DATE=")]
        assert "release date" in block

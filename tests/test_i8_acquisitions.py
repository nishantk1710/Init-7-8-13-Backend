"""New 80-series purchases, which justification answers each, and the
deleted/blocked PO-line rule.

No database. Which PR items and PO lines are new purchases (since 27-Oct-2026
the new-buy PR, plus direct PO lines with no PR behind it), and which purchase a
NEW_ACQUISITION justification answered -- the Justifications screen's Purchase
column. Rows are hand-built in the ``v_eban`` / ``v_ekpo`` / ``v_ekko`` /
``v_resb`` column names.

The bought-new exception these purchases also fed (UNJUSTIFIED_ACQUISITION) was
removed with the exception queue on 3-Nov-2026, and its tests with it.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import date
from decimal import Decimal

import pytest

from app.initiatives.i8 import staged
from app.initiatives.i8.acquisitions import (
    PURCHASE_ORDER,
    PURCHASE_REQUISITION,
    JustificationRecord,
    NewAcquisition,
    find_new_acquisitions,
    is_direct_purchase_line,
    is_new_buy_requisition,
    is_new_purchase_line,
    load_new_acquisitions,
    purchases_answered,
    repair_order_references,
)
from app.initiatives.i8.config import I8Settings
from app.initiatives.i8.register import is_deleted_line

CFG = I8Settings(_env_file=None)
BOUGHT = date(2026, 5, 10)
NON_ORDER = frozenset({"WK", "MK", "AN", "FPA"})  # I13_NON_ORDER_DOC_TYPES' default


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


# --- which purchase a justification answered ---------------------------------------


def reason(recorded_on: date, *, id: str = "J1", exception_id: str | None = None, **over) -> JustificationRecord:
    values = dict(material_id="8000005632", plant="1300", recorded_on=recorded_on, id=id, exception_id=exception_id)
    values.update(over)
    return JustificationRecord(**values)


class TestPurchasesAnswered:
    def test_a_reason_answers_a_purchase_of_the_part_at_the_plant_within_the_window(self) -> None:
        answered = purchases_answered([reason(date(2026, 5, 1))], [purchase()], window_days=30)
        assert answered["J1"].purchasing_document == "1000201234"

    def test_outside_the_window_or_another_plant_answers_nothing(self) -> None:
        assert purchases_answered([reason(date(2026, 3, 1))], [purchase()], window_days=30) == {}
        assert purchases_answered([reason(date(2026, 5, 1), plant="1500")], [purchase()], window_days=30) == {}

    def test_the_nearest_purchase_wins_and_one_raised_after_the_reason_first(self) -> None:
        before = purchase(purchasing_document="1000200001", raised_at=date(2026, 5, 6))
        after = purchase(purchasing_document="1000200002", raised_at=date(2026, 5, 14))
        answered = purchases_answered([reason(date(2026, 5, 10))], [before, after], window_days=30)
        assert answered["J1"].purchasing_document == "1000200002"

    def test_a_reason_recorded_against_a_bought_new_finding_answers_that_purchase(self) -> None:
        """Ids of the removed exception queue, on reasons recorded before 3-Nov-2026."""
        named = purchase(purchasing_document="1000209999", raised_at=date(2025, 1, 1))
        record = reason(date(2026, 5, 10), exception_id="EX-UNJUSTIFIED_ACQUISITION-PR-1000209999-10")
        answered = purchases_answered([record], [purchase(), named], window_days=30)
        assert answered["J1"].purchasing_document == "1000209999"

    def test_a_reason_with_no_id_is_skipped(self) -> None:
        assert purchases_answered([reason(date(2026, 5, 10), id=None)], [purchase()], window_days=30) == {}


class TestConfigurationDefaults:
    def test_new_settings_have_working_defaults(self) -> None:
        cfg = I8Settings(_env_file=None)
        assert cfg.po_deleted_indicator == "L"
        assert cfg.po_blocked_indicator == "S"
        assert cfg.delivery_completed_indicator == "X"
        assert cfg.new_purchase_item_category == "0"
        assert cfg.justification_window_days == 30
        assert cfg.attestation_max_quantity == 1000

    def test_the_settings_of_the_removed_checks_are_gone(self) -> None:
        """3-Nov-2026: the exception queue went, and its settings with it."""
        for name in (
            "justification_cutover_date",
            "declaration_grace_days",
            "session_check_start_date",
            "notification_types",
            "complete_flow_enabled",
        ):
            assert name not in I8Settings.model_fields, name
        text = pathlib.Path(".env.example").read_text(encoding="utf-8")
        for variable in ("I8_JUSTIFICATION_CUTOVER_DATE=", "I8_NOTIFICATION_TYPES=", "I8_COMPLETE_FLOW_ENABLED="):
            assert variable not in text, variable

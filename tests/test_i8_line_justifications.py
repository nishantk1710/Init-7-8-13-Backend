"""FR-7 on the register -- which justifications belong to which repair line,
and what the register row carries now the Declaration Queue and Justifications
screens are folded into it.

No database. Every rule is a pure function of repair lines, justification
records and the exception queue as built.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from app.api.i8.mappers import repair_chain
from app.initiatives.i8.acquisitions import JustificationRecord, NewAcquisition
from app.initiatives.i8.config import I8Settings
from app.initiatives.i8.declarations import DeclarationRow
from app.initiatives.i8.exceptions import ExceptionType, unjustified_acquisitions
from app.initiatives.i8.line_justifications import (
    MISSING,
    RECORDED,
    LineJustification,
    by_line,
    was_out_on,
)
from app.initiatives.i8.register import RepairLine

CFG = I8Settings(_env_file=None)
RAISED = date(2026, 5, 1)
BACK = date(2026, 6, 15)


def repair(**overrides) -> RepairLine:
    """A repair of 8000005632 at 1300, out from RAISED until it comes back."""
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
        delivery_completed=False,
        pr_number="10012345",
        pr_item="10",
        requisitioner="TMOKOENA",
        doc_type="ZREP",
        corroborated_by_doc_type=True,
        has_po_header=True,
        vendor="400093",
        vendor_name=None,
        raised_at=RAISED,
        po_date=RAISED,
        due_date=date(2026, 6, 1),
        schedule_lines=1,
        dispatched_at=None,
        dispatched_qty=Decimal(0),
        received_at=None,
        received_qty=Decimal(0),
        reversals=0,
        lead_time_days=None,
        repair_status="PO Issued",
        receipt_status="Not Yet Shipped",
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


def reason(recorded_on: date, **overrides) -> JustificationRecord:
    defaults = dict(
        material_id="8000005632",
        plant="1300",
        recorded_on=recorded_on,
        id="J-1",
        reason_category="URGENT_BREAKDOWN",
        free_text="Line down; the repaired unit is weeks away.",
        author="T. Mokoena",
        recorded_at=datetime.combine(recorded_on, datetime.min.time(), timezone.utc),
        session_id="S-1",
    )
    defaults.update(overrides)
    return JustificationRecord(**defaults)


def purchase(raised_at: date = date(2026, 5, 10)) -> NewAcquisition:
    return NewAcquisition(
        purchasing_document="4100100001",
        item="10",
        material_id="8000005632",
        plant="1300",
        description="PUMP, CENTRIFUGAL",
        ordered_qty=Decimal(1),
        raised_at=raised_at,
    )


# --- when a line counts as out ---------------------------------------------


class TestWasOutOn:
    def test_from_the_day_it_was_raised(self) -> None:
        assert was_out_on(repair(), RAISED)

    def test_not_before_it_was_raised(self) -> None:
        assert not was_out_on(repair(), date(2026, 4, 30))

    def test_until_the_day_before_it_came_back(self) -> None:
        line = repair(received_at=BACK)
        assert was_out_on(line, date(2026, 6, 14))
        assert not was_out_on(line, BACK)

    def test_an_open_line_is_out_on_any_later_day(self) -> None:
        assert was_out_on(repair(), date(2026, 10, 8))

    def test_an_undated_line_is_never_out(self) -> None:
        assert not was_out_on(repair(raised_at=None), RAISED)


# --- which justifications belong to a line ---------------------------------


class TestRecorded:
    def test_a_reason_recorded_while_the_line_was_out(self) -> None:
        line = repair()
        index = by_line([line], [reason(date(2026, 5, 20))], [])
        assert index[line.key].status == RECORDED
        assert [r.id for r in index[line.key].recorded] == ["J-1"]

    def test_a_reason_recorded_after_the_unit_came_back_is_not_this_lines(self) -> None:
        line = repair(received_at=BACK)
        assert by_line([line], [reason(date(2026, 7, 1))], []) == {}

    def test_another_plant_is_another_shelf(self) -> None:
        assert by_line([repair()], [reason(date(2026, 5, 20), plant="1500")], []) == {}

    def test_another_part_is_not_this_lines(self) -> None:
        assert by_line([repair()], [reason(date(2026, 5, 20), material_id="8000000001")], []) == {}

    def test_a_line_with_no_plant_matches_nothing(self) -> None:
        assert by_line([repair(plant=None)], [reason(date(2026, 5, 20))], []) == {}

    def test_newest_first(self) -> None:
        line = repair()
        index = by_line(
            [line],
            [reason(date(2026, 5, 5), id="J-old"), reason(date(2026, 9, 1), id="J-new")],
            [],
        )
        assert [r.id for r in index[line.key].recorded] == ["J-new", "J-old"]

    def test_one_reason_lands_on_every_line_that_was_out(self) -> None:
        """Two repairs of the same part out at once: the reason is about the
        part at the plant, so both lines show it."""
        first, second = repair(item="10"), repair(item="20")
        index = by_line([first, second], [reason(date(2026, 5, 20))], [])
        assert set(index) == {first.key, second.key}

    def test_a_line_with_neither_is_absent(self) -> None:
        assert by_line([repair()], [], []) == {}


class TestMissing:
    def test_an_unjustified_purchase_on_the_line(self) -> None:
        line = repair()
        exceptions = unjustified_acquisitions([purchase()], [line], [], window_days=30)
        index = by_line([line], [], exceptions)
        assert index[line.key].status == MISSING
        [item] = index[line.key].unjustified
        assert item.type == ExceptionType.UNJUSTIFIED_ACQUISITION.value
        assert (item.acquisition_document, item.acquisition_item) == ("4100100001", "10")

    def test_missing_wins_over_recorded(self) -> None:
        """One purchase explained, another not: the one to act on is shown."""
        line = repair()
        exceptions = unjustified_acquisitions([purchase()], [line], [], window_days=30)
        index = by_line([line], [reason(date(2026, 9, 1))], exceptions)
        assert index[line.key].status == MISSING
        assert len(index[line.key].recorded) == 1

    def test_the_register_and_the_queue_agree(self) -> None:
        """A reason that answers the purchase clears both, because the register
        reads the queue rather than re-deriving it."""
        line = repair()
        answered = reason(date(2026, 5, 10))
        exceptions = unjustified_acquisitions([purchase()], [line], [answered], window_days=30)
        assert exceptions == []
        assert by_line([line], [answered], exceptions)[line.key].status == RECORDED

    def test_only_unjustified_acquisitions_count(self) -> None:
        from app.initiatives.i8.exceptions import ExceptionItem

        line = repair()
        missing_attestation = ExceptionItem(
            id="EX-MISSING_ATTESTATION-4500001234-10",
            type=ExceptionType.MISSING_ATTESTATION.value,
            severity="warning",
            material_id=line.material_id,
            description=None,
            plant=line.plant,
            purchasing_document=line.purchasing_document,
            item=line.item,
            title="",
            detail="",
            raised_at=RAISED,
            is_open_repair=True,
        )
        assert by_line([line], [], [missing_attestation]) == {}


# --- what the register row carries -----------------------------------------


def declaration(**overrides) -> DeclarationRow:
    defaults = dict(
        id="D-4500001234-10",
        material_id="8000005632",
        description="Pump repair",
        plant="1300",
        pr_number="10012345",
        pr_item="10",
        requester="TMOKOENA",
        has_active_repair=True,
        related_repair_id="4500001234-10",
        status="Flagged",
        declared_by="R. Kruger",
        declared_at=datetime(2026, 9, 12, 9, 30, tzinfo=timezone.utc),
        condition="Scrap",
        next_action="Assessed as Scrap but sent for repair anyway.",
        created_at=RAISED,
    )
    defaults.update(overrides)
    return DeclarationRow(**defaults)


class TestTheRegisterRow:
    def test_the_declaration_travels_on_the_row(self) -> None:
        row = repair_chain(repair(), CFG, declaration=declaration())
        assert row.declaration_status == "Flagged"
        assert row.declared_by == "R. Kruger"
        assert row.declared_at == datetime(2026, 9, 12, 9, 30, tzinfo=timezone.utc)
        assert row.condition == "Scrap"
        assert row.next_action.startswith("Assessed as Scrap")

    def test_the_requester_comes_from_the_line(self) -> None:
        assert repair_chain(repair(), CFG).requester == "TMOKOENA"

    def test_no_declaration_reads_required_with_nothing_declared(self) -> None:
        row = repair_chain(repair(), CFG)
        assert row.declaration_status == "Required"
        assert (row.declared_by, row.declared_at, row.condition, row.next_action) == (
            None,
            None,
            None,
            None,
        )

    def test_a_recorded_justification(self) -> None:
        value = LineJustification(recorded=(reason(date(2026, 5, 20)),))
        cell = repair_chain(repair(), CFG, justification=value).justification
        assert cell is not None
        assert cell.status == "RECORDED"
        [entry] = cell.entries
        assert (entry.reason_category, entry.author, entry.session_id) == (
            "URGENT_BREAKDOWN",
            "T. Mokoena",
            "S-1",
        )
        assert cell.unjustified_purchases == []

    def test_a_missing_justification_names_the_purchase(self) -> None:
        line = repair()
        [item] = unjustified_acquisitions([purchase()], [line], [], window_days=30)
        cell = repair_chain(line, CFG, justification=LineJustification(unjustified=(item,))).justification
        assert cell is not None
        assert cell.status == "MISSING"
        [bought] = cell.unjustified_purchases
        assert bought.exception_id == item.id
        assert (bought.purchase.document_number, bought.purchase.line) == ("4100100001", "10")
        assert bought.raised_at == date(2026, 5, 10)
        assert bought.pre_automation is False

    def test_no_justification_is_null_not_empty(self) -> None:
        assert repair_chain(repair(), CFG).justification is None

    def test_wire_names_are_camel_case(self) -> None:
        value = LineJustification(recorded=(reason(date(2026, 5, 20)),))
        wire = repair_chain(
            repair(), CFG, declaration=declaration(), justification=value
        ).model_dump(by_alias=True)
        assert {"declaredBy", "declaredAt", "condition", "nextAction", "requester"} <= set(wire)
        assert set(wire["justification"]) == {"status", "entries", "unjustifiedPurchases"}
        assert {"reasonCategory", "freeText", "recordedAt", "sessionId"} <= set(
            wire["justification"]["entries"][0]
        )

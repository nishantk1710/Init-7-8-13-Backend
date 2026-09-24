"""FR-8 -- UNJUSTIFIED_ACQUISITION, and the deleted/blocked PO-line rule.

No database. The detector is a pure function of purchase lines, repair lines
and justifications, and every decision it makes is pinned here: what counts as
a new purchase, what "a repair was open at the time" means, which
justifications answer a purchase, and how a pre-cutover purchase is labelled.
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.initiatives.i8.acquisitions import (
    JustificationRecord,
    NewAcquisition,
    find_new_acquisitions,
    is_new_purchase_line,
    open_repair_at,
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

CFG = I8Settings(_env_file=None)
BOUGHT = date(2026, 5, 10)


def repair(**overrides) -> RepairLine:
    """An open repair of 8000005632 at 1300, raised before BOUGHT."""
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


def purchase(**overrides) -> NewAcquisition:
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


def ekpo_row(**overrides) -> dict:
    defaults = dict(
        ebeln="4100100001", ebelp="10", matnr="8000005632", werks="1300",
        pstyp="0", txz01="PUMP", menge=Decimal(1), erdat=BOUGHT, loekz=None,
    )
    defaults.update(overrides)
    return defaults


def raised(acquisitions, lines, justifications=(), *, window=30, cutover=None):
    return unjustified_acquisitions(
        acquisitions, lines, justifications, window_days=window, cutover=cutover
    )


# --- which purchase lines are new acquisitions --------------------------


class TestWhatCountsAsANewPurchase:
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

    def test_find_keeps_only_new_purchases(self) -> None:
        rows = [ekpo_row(), ekpo_row(ebelp="20", pstyp="3"), ekpo_row(ebelp="30", loekz="L")]
        found = find_new_acquisitions(rows, CFG)
        assert [a.key for a in found] == [("4100100001", "10")]
        assert found[0].raised_at == BOUGHT


# --- the deleted / blocked PO-line rule ----------------------------------


class TestDeletedAndBlockedLines:
    def test_the_deleted_value_is_read_from_configuration(self) -> None:
        assert is_deleted_line({"loekz": "L"}, CFG) is True
        assert is_deleted_line({"loekz": "S"}, CFG) is False
        assert is_deleted_line({"loekz": None}, CFG) is False
        cfg = I8Settings(_env_file=None, po_deleted_indicator="X")
        assert is_deleted_line({"loekz": "X"}, cfg) is True

    def test_no_loekz_or_item_category_literal_in_the_logic(self) -> None:
        """Same discipline as the repair convention: the values are settings."""
        for name in ("register.py", "acquisitions.py"):
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
                assert not literals & {"L", "S", "0"}, f"{name}: {literals} compared literally"


# --- was a repair open when the purchase was raised ----------------------


class TestOpenRepairAtThePurchase:
    def index(self, *lines):
        return repairs_by_material_plant(lines)

    def test_an_open_repair_raised_before_the_purchase(self) -> None:
        line = repair()
        assert open_repair_at(purchase(), self.index(line)) is line

    def test_a_repair_raised_the_same_day_counts(self) -> None:
        line = repair(raised_at=BOUGHT)
        assert open_repair_at(purchase(), self.index(line)) is line

    def test_a_repair_raised_after_the_purchase_does_not(self) -> None:
        assert open_repair_at(purchase(), self.index(repair(raised_at=date(2026, 5, 11)))) is None

    def test_a_repair_already_back_does_not(self) -> None:
        back = repair(received_at=date(2026, 5, 9))
        assert open_repair_at(purchase(), self.index(back)) is None

    def test_a_repair_that_came_back_later_was_still_out_at_the_time(self) -> None:
        later = repair(received_at=date(2026, 5, 20))
        assert open_repair_at(purchase(), self.index(later)) is later

    def test_another_plant_is_another_shelf(self) -> None:
        assert open_repair_at(purchase(), self.index(repair(plant="1500"))) is None

    def test_the_soonest_due_repair_is_the_one_reported(self) -> None:
        late = repair(item="10", due_date=date(2026, 7, 1))
        soon = repair(item="20", due_date=date(2026, 5, 20))
        undated = repair(item="30", due_date=None)
        assert open_repair_at(purchase(), self.index(late, undated, soon)) is soon

    def test_an_undated_purchase_cannot_be_placed_in_time(self) -> None:
        assert open_repair_at(purchase(raised_at=None), self.index(repair())) is None


# --- the exception ---------------------------------------------------------


class TestUnjustifiedAcquisition:
    def test_raised_when_a_repair_was_open_and_nobody_said_why(self) -> None:
        [item] = raised([purchase()], [repair()])
        assert item.type == ExceptionType.UNJUSTIFIED_ACQUISITION.value
        assert item.id == "EX-UNJUSTIFIED_ACQUISITION-4100100001-10"
        assert (item.acquisition_document, item.acquisition_item) == ("4100100001", "10")
        # The repair line fields point at the repair it overlapped.
        assert (item.purchasing_document, item.item) == ("4500001234", "10")
        assert item.severity == "warning"
        assert item.raised_at == BOUGHT
        assert item.is_open_repair is True
        assert "within 30 days" in item.detail

    def test_not_raised_when_no_repair_was_open(self) -> None:
        """Buying a part nobody is repairing is ordinary procurement."""
        assert raised([purchase()], [repair(received_at=date(2026, 5, 2))]) == []

    @pytest.mark.parametrize("recorded", [date(2026, 4, 10), BOUGHT, date(2026, 6, 9)])
    def test_a_justification_inside_the_window_answers_it(self, recorded) -> None:
        reason = JustificationRecord(material_id="8000005632", plant="1300", recorded_on=recorded)
        assert raised([purchase()], [repair()], [reason]) == []

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
            exception_id="EX-UNJUSTIFIED_ACQUISITION-4100100001-10",
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

    def test_after_the_cutover_it_is_a_warning(self) -> None:
        [item] = raised([purchase()], [repair()], cutover=date(2026, 1, 1))
        assert item.pre_automation is False
        assert item.severity == "warning"

    def test_it_is_now_a_raised_type(self) -> None:
        assert ExceptionType.UNJUSTIFIED_ACQUISITION in RAISED_BY_I8
        assert ExceptionType.MISSING_SESSION_ID not in RAISED_BY_I8

    def test_both_detectors_share_one_queue_and_one_set_of_counts(self) -> None:
        line = repair()
        coverage = AttestationCoverage(covered={}, uncovered={line.key}, window_days=30)
        items, stats = build_exceptions(
            [line],
            coverage,
            acquisitions=[purchase(), purchase(item="20", raised_at=date(2026, 4, 1))],
            justification_window_days=30,
            justification_cutover=date(2026, 5, 5),
        )
        assert stats.by_type == {"MISSING_ATTESTATION": 1, "UNJUSTIFIED_ACQUISITION": 1}
        assert stats.acquisitions_checked == 2
        assert stats.total == 2
        assert stats.pre_automation == 0
        assert stats.actionable == 2
        assert stats.justification_cutover_date == date(2026, 5, 5)
        assert {i.type for i in items} == {"MISSING_ATTESTATION", "UNJUSTIFIED_ACQUISITION"}

    def test_a_blocked_repair_still_counts_as_open(self) -> None:
        """Blocked is not cancelled -- the unit is still away."""
        [item] = raised([purchase()], [replace(repair(), po_blocked=True)])
        assert item.is_open_repair is True


class TestConfigurationDefaults:
    def test_new_settings_have_working_defaults(self) -> None:
        cfg = I8Settings(_env_file=None)
        assert cfg.po_deleted_indicator == "L"
        assert cfg.po_blocked_indicator == "S"
        assert cfg.new_purchase_item_category == "0"
        assert cfg.justification_window_days == 30
        assert cfg.justification_cutover_date_value is None
        assert cfg.attestation_max_quantity == 1000

    def test_a_malformed_justification_cutover_fails_loudly(self) -> None:
        with pytest.raises(ValueError):
            I8Settings(_env_file=None, justification_cutover_date="01-10-2026").justification_cutover_date_value

"""I08 FR-6 -- does a repairable unit already exist?

No database. The rule takes loaded rows and returns a verdict, which is the
whole reason it was written that way: the assistant, the standalone endpoint and
these tests all exercise the same function and cannot drift apart.

The cases that matter most here are the *absences*. "No repairable in stock" and
"no source told us about stock" both render as no number on a screen, and only
one of them should stop somebody buying a part.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.initiatives.i8 import repairable_unit
from app.initiatives.i8.repairable_unit import UnitSource

TODAY = date(2026, 7, 31)


def _universe_row(**overrides):
    """A universe row with only the fields FR-6 reads set to something sensible."""
    from app.initiatives.i8.universe import UniverseRow

    defaults = dict(
        material_id="8000005632",
        plant="1300",
        description="Pump, centrifugal",
        material_type="ZREP",
        stock_on_hand=Decimal("0"),
        storage_locations=1,
        reorder_point=None,
        mrp_type="ND",
        planned_delivery_days=30,
        criticality=None,
        open_repair_lines=0,
        qty_under_repair=Decimal("0"),
        in_material_master=True,
    )
    defaults.update(overrides)
    return UniverseRow(**defaults)


def _repair_line(**overrides):
    """An OPEN repair line -- received_at None -- with the FR-6 fields set."""
    from app.initiatives.i8.register import RepairLine

    defaults = dict(
        purchasing_document="4500001234",
        item="00010",
        material_id="8000005632",
        plant="1300",
        description="Pump repair",
        ordered_qty=Decimal("1"),
        unit="EA",
        net_price=Decimal("1000"),
        item_category="3",
        delivery_completed=False,
        pr_number=None,
        pr_item=None,
        requisitioner="MILLERJ",
        doc_type="ZREP",
        corroborated_by_doc_type=True,
        has_po_header=True,
        vendor="0000100234",
        vendor_name="Acme Rotating",
        raised_at=date(2026, 5, 1),
        po_date=date(2026, 5, 1),
        due_date=date(2026, 8, 3),
        schedule_lines=1,
        dispatched_at=None,
        dispatched_qty=Decimal("0"),
        received_at=None,
        received_qty=Decimal("0"),
        reversals=0,
        lead_time_days=30,
        repair_status="PO Issued",
        receipt_status="Not Yet Shipped",
        overdue_status="On Time",
        lead_time_status="WITHIN_LEAD_TIME",
        qty_under_repair=Decimal("1"),
        days_open=91,
        days_elapsed=91,
        days_over_lead_time=61,
        days_at_vendor=None,
        days_in_current_stage=91,
        days_remaining=3,
        aging_bucket="61+",
    )
    defaults.update(overrides)
    return RepairLine(**defaults)


def _assess(universe_rows=(), repair_lines=(), material="8000005632", plant="1300"):
    return repairable_unit.assess(
        material_id=material,
        plant=plant,
        universe_rows=universe_rows,
        repair_lines=repair_lines,
        today=TODAY,
    )


class TestNotARepairablePart:
    def test_a_material_outside_the_universe_is_answered_not_failed(self) -> None:
        """A valid, useful answer -- the assistant simply has nothing to offer."""
        verdict = _assess(universe_rows=[], material="1000000123")
        assert verdict.is_repairable_material is False
        assert verdict.exists is False
        assert "not an 80-series repairable part" in verdict.headline

    def test_nothing_is_reported_as_zero_when_nothing_was_checked(self) -> None:
        """Zeros would read as "we looked and found none". We did not look."""
        verdict = _assess(universe_rows=[], material="1000000123")
        assert verdict.stock_on_hand is None
        assert verdict.open_repair_lines == 0
        assert verdict.evidence == ()


class TestStock:
    def test_stock_on_the_shelf_counts_as_an_existing_unit(self) -> None:
        verdict = _assess(universe_rows=[_universe_row(stock_on_hand=Decimal("2"))])
        assert verdict.exists is True
        assert UnitSource.STOCK in verdict.sources
        assert "2 in stock" in verdict.headline

    def test_zero_stock_is_not_an_existing_unit(self) -> None:
        verdict = _assess(universe_rows=[_universe_row(stock_on_hand=Decimal("0"))])
        assert verdict.exists is False
        assert verdict.has_stock is False

    def test_unknown_stock_is_not_zero(self) -> None:
        """The distinction the whole module is built around. No MARD row means
        no source told us, and that must not read as "none in stock"."""
        verdict = _assess(universe_rows=[_universe_row(stock_on_hand=None)])
        assert verdict.stock_on_hand is None
        assert verdict.stock_is_unknown is True
        assert verdict.has_stock is False

    def test_unknown_stock_says_so_and_suggests_a_physical_check(self) -> None:
        verdict = _assess(universe_rows=[_universe_row(stock_on_hand=None)])
        assert "cannot tell" in verdict.headline
        assert any("not that stock is zero" in note for note in verdict.caveats)

    def test_stock_sums_across_plants_only_where_it_is_known(self) -> None:
        """One plant with 2 and another with no record reports 2, not a guess."""
        verdict = _assess(
            universe_rows=[
                _universe_row(plant="1300", stock_on_hand=Decimal("2")),
                _universe_row(plant="1500", stock_on_hand=None),
            ],
            plant=None,
        )
        assert verdict.stock_on_hand == Decimal("2")

    def test_stock_stays_unknown_when_no_row_knows(self) -> None:
        verdict = _assess(
            universe_rows=[
                _universe_row(plant="1300", stock_on_hand=None),
                _universe_row(plant="1500", stock_on_hand=None),
            ],
            plant=None,
        )
        assert verdict.stock_on_hand is None


class TestOpenRepairs:
    def test_an_open_repair_counts_as_a_unit_coming_back(self) -> None:
        verdict = _assess(
            universe_rows=[_universe_row()], repair_lines=[_repair_line()]
        )
        assert verdict.exists is True
        assert UnitSource.ON_REPAIR_ORDER in verdict.sources
        assert verdict.open_repair_lines == 1

    def test_a_closed_repair_does_not_count(self) -> None:
        """The unit already came back; it is stock now, not inbound."""
        verdict = _assess(
            universe_rows=[_universe_row()],
            repair_lines=[_repair_line(received_at=date(2026, 6, 1))],
        )
        assert verdict.open_repair_lines == 0
        assert UnitSource.ON_REPAIR_ORDER not in verdict.sources

    def test_the_soonest_due_date_is_what_a_requester_decides_against(self) -> None:
        verdict = _assess(
            universe_rows=[_universe_row()],
            repair_lines=[
                _repair_line(item="00010", due_date=date(2026, 9, 30)),
                _repair_line(item="00020", due_date=date(2026, 8, 3)),
            ],
        )
        assert verdict.soonest_due_date == date(2026, 8, 3)
        assert "2026-08-03" in verdict.headline

    def test_a_line_with_no_due_date_says_so_rather_than_implying_on_time(self) -> None:
        """61 of 788 open lines. "Nobody promised a date" is not "on time"."""
        verdict = _assess(
            universe_rows=[_universe_row()],
            repair_lines=[_repair_line(due_date=None)],
        )
        assert verdict.soonest_due_date is None
        assert verdict.evidence[0].days_overdue is None
        assert "no promised return date" in verdict.headline


class TestOverdue:
    def test_overdue_is_measured_against_the_caller_s_today(self) -> None:
        """Not against a snapshot built at process start-up. The assistant
        states this to somebody at the moment they are deciding."""
        verdict = _assess(
            universe_rows=[_universe_row()],
            repair_lines=[_repair_line(due_date=date(2026, 7, 1))],
        )
        assert verdict.evidence[0].days_overdue == 30
        assert verdict.overdue_lines == 1

    def test_a_future_due_date_is_not_overdue(self) -> None:
        verdict = _assess(
            universe_rows=[_universe_row()],
            repair_lines=[_repair_line(due_date=date(2026, 12, 1))],
        )
        assert verdict.evidence[0].days_overdue is None
        assert verdict.overdue_lines == 0

    def test_overdue_lines_are_flagged_as_a_plan_not_a_commitment(self) -> None:
        verdict = _assess(
            universe_rows=[_universe_row()],
            repair_lines=[_repair_line(due_date=date(2026, 7, 1))],
        )
        assert any("plan rather than a commitment" in note for note in verdict.caveats)


class TestWhatItMayNotSay:
    def test_it_never_claims_the_vendor_holds_the_unit(self) -> None:
        """Zero of 788 open lines carry a dispatch movement on this extract, so
        FR-5(a)'s "the vendor has it" has no data behind it."""
        verdict = _assess(
            universe_rows=[_universe_row()], repair_lines=[_repair_line()]
        )
        assert verdict.evidence[0].dispatched is False
        assert "vendor has" not in verdict.headline.lower()
        assert any("not that the unit has physically reached" in note for note in verdict.caveats)

    def test_a_dispatched_line_drops_the_caveat(self) -> None:
        """The branch is implemented for a live client that posts 541 movements
        -- finding out at cutover that it cannot describe them would be worse."""
        verdict = _assess(
            universe_rows=[_universe_row()],
            repair_lines=[_repair_line(dispatched_at=date(2026, 5, 10))],
        )
        assert verdict.evidence[0].dispatched is True
        assert not any("physically reached" in note for note in verdict.caveats)

    def test_a_vendor_with_no_master_record_is_shown_by_code(self) -> None:
        """106 vendors against 454 suppliers. A code, never an invented name."""
        verdict = _assess(
            universe_rows=[_universe_row()],
            repair_lines=[_repair_line(vendor_name=None)],
        )
        assert verdict.evidence[0].vendor == "0000100234"
        assert verdict.evidence[0].vendor_name is None
        assert any("by code only" in note for note in verdict.caveats)


class TestNothingExists:
    def test_no_stock_and_no_repair_is_a_clear_negative(self) -> None:
        verdict = _assess(universe_rows=[_universe_row(stock_on_hand=Decimal("0"))])
        assert verdict.exists is False
        assert "No repairable unit exists" in verdict.headline


class TestMaterialMatching:
    def test_the_material_number_is_normalised_on_the_way_in(self) -> None:
        """Ruling 5.1 -- the padded form and the stripped form are one part."""
        verdict = _assess(
            universe_rows=[_universe_row(stock_on_hand=Decimal("1"))],
            material="000000008000005632",
        )
        assert verdict.material_id == "8000005632"
        assert verdict.exists is True

    def test_another_material_s_rows_are_not_counted(self) -> None:
        verdict = _assess(
            universe_rows=[
                _universe_row(material_id="8000009999", stock_on_hand=Decimal("5"))
            ]
        )
        assert verdict.is_repairable_material is False

    def test_another_plant_s_rows_are_not_counted(self) -> None:
        verdict = _assess(
            universe_rows=[_universe_row(plant="1500", stock_on_hand=Decimal("5"))],
            plant="1300",
        )
        assert verdict.is_repairable_material is False

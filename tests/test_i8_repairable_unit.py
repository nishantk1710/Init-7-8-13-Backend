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
    """A repair line SAP still has open -- the only kind the register holds --
    with the FR-6 fields set."""
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
        lead_time_days=30,
        repair_status="PO Issued",
        overdue_status="On Time",
        lead_time_status="WITHIN_LEAD_TIME",
        days_open=91,
        days_elapsed=91,
        days_over_lead_time=61,
        days_at_vendor=None,
        days_in_current_stage=91,
        days_remaining=3,
        aging_bucket="61+",
    )
    defaults.update(overrides)
    # The register's own rule: the whole ordered quantity is out until SAP
    # closes the line.
    defaults.setdefault("qty_under_repair", defaults["ordered_qty"])
    return RepairLine(**defaults)


def _assess(
    universe_rows=(),
    repair_lines=(),
    material="8000005632",
    plant="1300",
    repair_requests=None,
    department_name=None,
):
    return repairable_unit.assess(
        material_id=material,
        plant=plant,
        universe_rows=universe_rows,
        repair_lines=repair_lines,
        today=TODAY,
        repair_requests=repair_requests,
        department_name=department_name,
        usable_conditions=("NEW", "REFURBISHD"),
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


# --- 27-Oct-2026: open repair PRs and the usable/damaged split ----------------


def _repair_request(**overrides):
    from tests.assistant.conftest import repair_request

    return repair_request(**overrides)


def _known(usable=None, damaged=None, source="MCHB", **conditions):
    """A universe row with condition rows: ``conditions`` maps code to units."""
    by_condition = tuple(sorted((c, Decimal(v)) for c, v in conditions.items()))
    return dict(
        usable_stock=Decimal(usable) if usable is not None else Decimal(0),
        damaged_stock=Decimal(damaged) if damaged is not None else Decimal(0),
        stock_by_condition=by_condition,
        stock_condition_source=source,
    )


class TestRepairRequests:
    """A repair PR with no PO yet: 108 on the 15-Oct extract, invisible to the
    PO-only register. Counted only when the caller passes them."""

    def test_a_pr_only_part_has_a_unit_only_when_requests_are_passed(self) -> None:
        rows = [_universe_row()]
        off = _assess(universe_rows=rows, repair_requests=None)
        on = _assess(universe_rows=rows, repair_requests=[_repair_request()])
        assert off.exists is False
        assert off.request_evidence == ()
        assert on.exists is True
        assert UnitSource.REPAIR_REQUESTED in on.sources
        assert on.open_repair_request_lines == 1

    def test_the_evidence_carries_what_the_record_states(self) -> None:
        names = {"10305": "Engineering"}
        verdict = _assess(
            universe_rows=[_universe_row()],
            repair_requests=[_repair_request()],
            department_name=names.get,
        )
        [item] = verdict.request_evidence
        assert item.purchase_requisition == "2000029113"
        assert item.item == "10"
        assert item.quantity == Decimal("1")
        assert item.requested_on == date(2025, 12, 4)
        assert item.days_since_requested == 239
        assert item.department == "10305"
        assert item.department_name == "Engineering"
        assert item.repair_order == "940000004981"

    def test_an_unnamed_department_is_shown_by_code(self) -> None:
        verdict = _assess(
            universe_rows=[_universe_row()],
            repair_requests=[_repair_request()],
            department_name=lambda code: None,
        )
        assert verdict.request_evidence[0].department == "10305"
        assert verdict.request_evidence[0].department_name is None

    def test_another_part_s_or_plant_s_requests_are_not_counted(self) -> None:
        verdict = _assess(
            universe_rows=[_universe_row()],
            repair_requests=[
                _repair_request(material_id="8000009999"),
                _repair_request(plant="1500"),
            ],
        )
        assert verdict.exists is False
        assert verdict.request_evidence == ()

    def test_one_request_headline_and_caveat(self) -> None:
        verdict = _assess(universe_rows=[_universe_row()], repair_requests=[_repair_request()])
        assert (
            "There is 1 repair requested, not yet ordered (requested 239 days ago)."
            in verdict.headline
        )
        assert (
            "A requested repair has no PO yet, so there is no date for its return."
            in verdict.caveats
        )

    def test_several_requests_name_the_oldest(self) -> None:
        verdict = _assess(
            universe_rows=[_universe_row()],
            repair_requests=[
                _repair_request(item="20", requested_on=date(2026, 7, 30)),
                _repair_request(item="10"),
                _repair_request(item="30", requested_on=None),
            ],
        )
        assert [e.item for e in verdict.request_evidence] == ["10", "20", "30"]
        assert (
            "There are 3 repairs requested, not yet ordered "
            "(the oldest requested 239 days ago)." in verdict.headline
        )

    def test_with_a_repair_po_too_the_no_date_caveat_is_not_given(self) -> None:
        verdict = _assess(
            universe_rows=[_universe_row()],
            repair_lines=[_repair_line()],
            repair_requests=[_repair_request(item="20")],
        )
        assert verdict.sources == (UnitSource.ON_REPAIR_ORDER, UnitSource.REPAIR_REQUESTED)
        assert verdict.open_repair_lines == 1
        assert not any("no PO yet" in note for note in verdict.caveats)
        # Each clause is its own sentence (it used to read ". and there").
        assert ". and there" not in verdict.headline
        assert ". There is 1 repair requested" in verdict.headline

    def test_counted_and_none_says_no_open_repair(self) -> None:
        counted = _assess(universe_rows=[_universe_row()], repair_requests=[])
        not_counted = _assess(universe_rows=[_universe_row()], repair_requests=None)
        assert counted.headline.endswith("no stock available and no open repair.")
        assert not_counted.headline.endswith("no stock available and no open repair PO.")


class TestUsableAndDamagedStock:
    """MCHB by default: NEW 1,667 / REFURBISHD 975 / DAMAGED 790 on 15-Oct."""

    def test_usable_stock_counts_and_damaged_is_named_but_not_counted(self) -> None:
        verdict = _assess(
            universe_rows=[
                _universe_row(
                    stock_on_hand=Decimal("3"),
                    **_known(usable=2, damaged=1, NEW=1, REFURBISHD=1, DAMAGED=1),
                )
            ]
        )
        assert verdict.exists is True
        assert verdict.sources == (UnitSource.STOCK,)
        assert verdict.stock_condition_known is True
        assert verdict.stock_source == "MCHB"
        assert (verdict.usable_stock, verdict.damaged_stock) == (Decimal(2), Decimal(1))
        assert verdict.stock_on_hand == Decimal(3)
        assert verdict.headline.startswith(
            "There are 2 usable in stock at plant 1300 (1 new, 1 refurbished); "
            "1 damaged unit is not counted."
        )

    def test_one_condition_gives_no_breakdown_and_no_damaged_clause(self) -> None:
        verdict = _assess(
            universe_rows=[_universe_row(stock_on_hand=Decimal("1"), **_known(usable=1, NEW=1))]
        )
        assert verdict.headline.startswith("There is 1 usable in stock at plant 1300.")
        assert "(" not in verdict.headline.split(".")[0]
        assert "damaged" not in verdict.headline

    def test_damaged_only_is_not_a_unit(self) -> None:
        verdict = _assess(
            universe_rows=[
                _universe_row(stock_on_hand=Decimal("2"), **_known(usable=0, damaged=2, DAMAGED=2))
            ]
        )
        assert verdict.exists is False
        assert verdict.has_stock is True  # the MARD total, unchanged
        assert verdict.has_usable_stock is False
        assert UnitSource.STOCK not in verdict.sources
        assert verdict.headline == (
            "No repairable unit exists for 8000005632 at plant 1300: no usable "
            "stock (2 damaged units are not counted) and no open repair PO."
        )

    def test_scrap_is_neither_usable_nor_damaged(self) -> None:
        verdict = _assess(
            universe_rows=[
                _universe_row(
                    stock_on_hand=None,
                    **_known(usable=0, damaged=0, source="MBEW", SCRAP=4),
                )
            ]
        )
        assert verdict.exists is False
        assert verdict.stock_source == "MBEW"
        assert verdict.headline.endswith("no stock available and no open repair PO.")
        # MBEW said something, so this is not "no stock record".
        assert not any("No stock record exists" in note for note in verdict.caveats)

    def test_condition_unknown_falls_back_to_the_total(self) -> None:
        verdict = _assess(universe_rows=[_universe_row(stock_on_hand=Decimal("3"))])
        assert verdict.exists is True
        assert verdict.stock_condition_known is False
        assert verdict.stock_source == "MARD"
        assert (verdict.usable_stock, verdict.damaged_stock) == (None, None)
        assert verdict.headline.startswith(
            "There are 3 in stock at plant 1300 (condition unknown)."
        )
        assert (
            "The stock record does not say which units are usable and which are damaged."
            in verdict.caveats
        )

    def test_a_zero_total_with_no_condition_rows_has_no_condition_caveat(self) -> None:
        """1,516 MARD rows with no MCHB rows on 15-Oct, all LABST 0."""
        verdict = _assess(universe_rows=[_universe_row(stock_on_hand=Decimal("0"))])
        assert verdict.stock_source == "MARD"
        assert not any("usable and which are damaged" in n for n in verdict.caveats)

    def test_no_stock_record_has_no_source(self) -> None:
        verdict = _assess(universe_rows=[_universe_row(stock_on_hand=None)])
        assert verdict.stock_source is None
        assert verdict.stock_condition_known is False
        assert "cannot tell" in verdict.headline

    def test_condition_rows_all_zero_are_known_and_zero(self) -> None:
        verdict = _assess(
            universe_rows=[
                _universe_row(stock_on_hand=Decimal("0"), **_known(usable=0, damaged=0, NEW=0))
            ]
        )
        assert verdict.stock_condition_known is True
        assert (verdict.usable_stock, verdict.damaged_stock) == (Decimal(0), Decimal(0))
        assert verdict.exists is False

    def test_a_plant_holding_unsplit_stock_makes_the_split_unknown(self) -> None:
        """Across plants, a total never mixes split and unsplit units."""
        verdict = _assess(
            universe_rows=[
                _universe_row(plant="1300", stock_on_hand=Decimal("2"), **_known(usable=2, NEW=2)),
                _universe_row(plant="1500", stock_on_hand=Decimal("1")),
            ],
            plant=None,
        )
        assert verdict.stock_condition_known is False
        assert verdict.stock_source == "MARD"
        assert verdict.stock_on_hand == Decimal(3)

    def test_a_plant_with_a_zero_total_does_not_spoil_the_split(self) -> None:
        verdict = _assess(
            universe_rows=[
                _universe_row(plant="1300", stock_on_hand=Decimal("2"), **_known(usable=2, NEW=2)),
                _universe_row(plant="1500", stock_on_hand=Decimal("0")),
            ],
            plant=None,
        )
        assert verdict.stock_condition_known is True
        assert verdict.usable_stock == Decimal(2)

    def test_the_breakdown_follows_the_configured_order(self) -> None:
        verdict = repairable_unit.assess(
            material_id="8000005632",
            plant="1300",
            universe_rows=[
                _universe_row(
                    stock_on_hand=Decimal("3"),
                    **_known(usable=3, NEW=1, REFURBISHD=2),
                )
            ],
            repair_lines=(),
            today=TODAY,
            usable_conditions=("REFURBISHD", "NEW"),
        )
        assert verdict.usable_breakdown == (("REFURBISHD", Decimal(2)), ("NEW", Decimal(1)))
        assert "(2 refurbished, 1 new)" in verdict.headline

    def test_the_breakdown_defaults_to_the_setting(self) -> None:
        verdict = repairable_unit.assess(
            material_id="8000005632",
            plant="1300",
            universe_rows=[
                _universe_row(stock_on_hand=Decimal("2"), **_known(usable=2, NEW=1, REFURBISHD=1))
            ],
            repair_lines=(),
            today=TODAY,
        )
        assert [c for c, _ in verdict.usable_breakdown] == ["NEW", "REFURBISHD"]

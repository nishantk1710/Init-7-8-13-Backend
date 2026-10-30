"""Acquired vs plan per plan (``plans.plan_outcomes``): which reservation items
stand for each captured plan, and what was issued against them."""

from datetime import date
from decimal import Decimal

from app.api.assistant.schemas import PlanModel
from app.api.i13.assistant import _with_outcome
from app.initiatives.i13.models import (
    GiLinkStatus,
    GrLinkStatus,
    LifecycleStatus,
    ReservationLedgerEntry,
    ReservationPrLinkStatus,
)
from app.initiatives.i13.plans import ConsumptionPlan, PlanSource, plan_outcomes
from app.shared.material_scope import MaterialScope


def _captured(plan_id: str = "P1", **overrides) -> ConsumptionPlan:
    """A captured plan with no SGTXT link, window 1-31 Oct 2026."""
    defaults = dict(
        plan_id=plan_id,
        session_id=f"S-{plan_id}",
        reservation_number="",
        reservation_item="",
        material="MAT1",
        plant="1000",
        requester="REQ1",
        purpose="test",
        planned_quantity=Decimal("10"),
        planned_use_date=date(2026, 10, 1),
        status="OPEN",
        source=PlanSource.CAPTURED,
        window_end=date(2026, 10, 31),
        captured_on=date(2026, 10, 1),
    )
    defaults.update(overrides)
    return ConsumptionPlan(**defaults)


def _entry(rspos: str = "0001", issued: str = "0", **overrides) -> ReservationLedgerEntry:
    defaults = dict(
        ledger_id=f"RESCHAIN-1000000000-{rspos}",
        reservation_number="1000000000",
        reservation_item=rspos,
        material="MAT1",
        plant="1000",
        reservation_quantity=Decimal("10"),
        requirement_date=date(2026, 10, 15),
        pr_number=None,
        pr_item=None,
        po_number=None,
        po_item=None,
        ordered_quantity=None,
        received_quantity=None,
        issued_quantity=Decimal(issued),
        first_gr_date=None,
        last_gr_date=None,
        first_issue_date=None,
        last_issue_date=None,
        procurement_issued_quantity=None,
        direct_store_issued_quantity=None,
        lifecycle_status=LifecycleStatus.RESERVED,
        reservation_pr_link_status=ReservationPrLinkStatus.NO_PR_REFERENCE,
        gr_link_status=GrLinkStatus.NOT_APPLICABLE,
        gi_link_status=GiLinkStatus.LINKED,
        gi_link_reason=None,
        material_scope=MaterialScope.OAR,
    )
    defaults.update(overrides)
    return ReservationLedgerEntry(**defaults)


def _by_id(outcomes):
    return {o.plan.plan_id: o for o in outcomes}


def test_an_unlinked_plan_takes_the_reservations_required_in_its_window() -> None:
    entries = [
        _entry("0001", "4"),
        _entry("0002", "6", requirement_date=date(2026, 10, 30)),
        _entry("0003", "9", requirement_date=date(2026, 11, 5)),  # after the window
        _entry("0004", "9", material="MAT2"),  # another material
    ]
    outcome = _by_id(plan_outcomes([_captured()], entries))["P1"]
    assert outcome.reservations == (("1000000000", "0001"), ("1000000000", "0002"))
    assert outcome.issued_quantity == 10


def test_an_sgtxt_link_wins_over_the_window() -> None:
    plan = _captured(linked_reservations=(("1000000000", "0003"),))
    entries = [_entry("0001", "4"), _entry("0003", "7", requirement_date=date(2026, 12, 1))]
    outcome = _by_id(plan_outcomes([plan], entries))["P1"]
    assert outcome.reservations == (("1000000000", "0003"),)
    assert outcome.issued_quantity == 7


def test_overlapping_plans_never_claim_the_same_reservation() -> None:
    """Two unlinked plans whose windows both cover the reservation: it counts
    for the more recently captured one only."""
    older = _captured("P1")
    newer = _captured("P2", captured_on=date(2026, 10, 2))
    outcomes = _by_id(plan_outcomes([older, newer], [_entry("0001", "10")]))
    assert outcomes["P1"].reservations == ()
    assert outcomes["P1"].issued_quantity == 0
    assert outcomes["P2"].reservations == (("1000000000", "0001"),)
    assert outcomes["P2"].issued_quantity == 10


def test_a_reservation_split_over_po_lines_is_counted_once() -> None:
    entries = [
        _entry("0001", "10", ledger_id="RESCHAIN-1000000000-0001-0", po_number="4500000000"),
        _entry("0001", "10", ledger_id="RESCHAIN-1000000000-0001-1", po_number="4500000001"),
    ]
    outcome = _by_id(plan_outcomes([_captured()], entries))["P1"]
    assert outcome.reservations == (("1000000000", "0001"),)
    assert outcome.issued_quantity == 10


def test_a_deleted_reservation_counts_only_if_something_was_issued() -> None:
    entries = [
        _entry("0001", "0", reservation_deleted=True),
        _entry("0002", "3", reservation_deleted=True),
    ]
    outcome = _by_id(plan_outcomes([_captured()], entries))["P1"]
    assert outcome.reservations == (("1000000000", "0002"),)
    assert outcome.issued_quantity == 3


def test_a_plan_nothing_stands_for_has_no_reservations() -> None:
    outcome = _by_id(plan_outcomes([_captured()], []))["P1"]
    assert outcome.reservations == ()
    assert outcome.issued_quantity == 0


def _plan_model() -> PlanModel:
    from datetime import datetime, timezone

    return PlanModel(
        id="P1", session_id="S-P1", material="MAT1", plant="1000", purpose="test",
        planned_quantity="10", status="OPEN", captured_by="REQ1",
        captured_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


def test_the_plan_list_row_carries_the_comparison() -> None:
    outcome = _by_id(plan_outcomes([_captured()], [_entry("0001", "4")]))["P1"]
    row = _with_outcome(_plan_model(), outcome).model_dump(by_alias=True)
    assert row["matchedReservations"] == ["1000000000/0001"]
    assert row["issuedQuantity"] == "4"
    assert row["acquiredVsPlanStatus"] == "BELOW_PLAN"
    assert row["acquiredVsPlanVarianceQuantity"] == "-6"


def test_a_plan_list_row_with_no_reservation_is_awaiting_one() -> None:
    outcome = _by_id(plan_outcomes([_captured()], []))["P1"]
    row = _with_outcome(_plan_model(), outcome).model_dump(by_alias=True)
    assert row["matchedReservations"] == []
    assert row["acquiredVsPlanStatus"] == "NO_RESERVATION"
    assert row["acquiredVsPlanVarianceQuantity"] is None


def test_a_plan_list_row_without_a_snapshot_is_left_unmeasured() -> None:
    row = _with_outcome(_plan_model(), None).model_dump(by_alias=True)
    assert row["acquiredVsPlanStatus"] is None
    assert row["issuedQuantity"] is None

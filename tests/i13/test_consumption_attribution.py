"""W6.4: deterministic consumption/ownership attribution.

Unit tests build small fake reservation rows/entries directly -- no
Postgres, no reservation_ledger stitching -- exercising
``ConsumptionAttributionService`` in isolation per the scenarios in the W6.4
implementation brief (A-H below map onto its section 10).
"""

from datetime import date
from decimal import Decimal

from app.initiatives.i13.consumption_attribution import ConsumptionAttributionService
from app.initiatives.i13.models import (
    ConsumptionAttributionSource,
    ConsumptionAttributionStatus,
    GiLinkStatus,
    GrLinkStatus,
    LifecycleStatus,
    ReservationLedgerEntry,
    ReservationPrLinkStatus,
)
from app.initiatives.i13.plans import ConsumptionPlan
from app.shared.material_scope import MaterialScope


def _plan(**overrides) -> ConsumptionPlan:
    defaults = dict(
        plan_id="PLAN-1",
        session_id="SESS-1",
        reservation_number="100001",
        reservation_item="0010",
        material="MAT1",
        plant="1000",
        requester="USER01",
        purpose="Breakdown replacement",
        planned_quantity=Decimal("10"),
        planned_use_date=None,
        status="OPEN",
    )
    defaults.update(overrides)
    return ConsumptionPlan(**defaults)


def _entry(**overrides) -> ReservationLedgerEntry:
    defaults = dict(
        ledger_id="RESCHAIN-100001-0010",
        reservation_number="100001",
        reservation_item="0010",
        material="MAT1",
        plant="1000",
        reservation_quantity=Decimal("10"),
        requirement_date=date(2026, 1, 1),
        pr_number="PR1",
        pr_item="0010",
        po_number="PO1",
        po_item="0010",
        ordered_quantity=Decimal("10"),
        received_quantity=Decimal("10"),
        issued_quantity=Decimal("5"),
        first_gr_date=None,
        last_gr_date=None,
        first_issue_date=None,
        last_issue_date=None,
        procurement_issued_quantity=Decimal("5"),
        direct_store_issued_quantity=Decimal("0"),
        lifecycle_status=LifecycleStatus.PARTIALLY_ISSUED,
        reservation_pr_link_status=ReservationPrLinkStatus.LINKED,
        gr_link_status=GrLinkStatus.RECEIVED,
        gi_link_status=GiLinkStatus.LINKED,
        gi_link_reason=None,
        material_scope=MaterialScope.OAR,
    )
    defaults.update(overrides)
    return ReservationLedgerEntry(**defaults)


def _resb_row(**overrides) -> dict:
    defaults = dict(Rsnum="100001", Rspos="0010", Aufnr="40001", Wempf="USER01")
    defaults.update(overrides)
    return defaults


class _FakeCostCentreProvider:
    def __init__(self, cost_centre: str | None) -> None:
        self._cost_centre = cost_centre

    def get_cost_centre(self, *, reservation_number, reservation_item, order_number) -> str | None:
        return self._cost_centre


# --- A: full attribution ----------------------------------------------------


def test_full_attribution_when_reservation_requester_order_and_cost_centre_all_resolve() -> None:
    entry = _entry()
    service = ConsumptionAttributionService(
        cost_centre_enabled=True, cost_centre_provider=_FakeCostCentreProvider("CC01")
    )
    result = service.attribute_entry(entry, [_resb_row()])

    assert result.reservation_number == "100001"
    assert result.reservation_item == "0010"
    assert result.requester_id == "USER01"
    assert result.order_number == "40001"
    assert result.cost_centre == "CC01"
    assert result.status is ConsumptionAttributionStatus.ATTRIBUTED
    assert result.source is ConsumptionAttributionSource.COST_CENTRE


# --- B: cost-centre path disabled -------------------------------------------


def test_cost_centre_disabled_still_attributes_reservation_requester_and_order() -> None:
    entry = _entry()
    service = ConsumptionAttributionService(
        cost_centre_enabled=False, cost_centre_provider=_FakeCostCentreProvider("CC01")
    )
    result = service.attribute_entry(entry, [_resb_row()])

    assert result.requester_id == "USER01"
    assert result.order_number == "40001"
    assert result.cost_centre is None
    assert result.status is ConsumptionAttributionStatus.ATTRIBUTED
    assert result.cost_centre_attribution_enabled is False


# --- C: missing cost centre (enabled but unavailable) -----------------------


def test_missing_cost_centre_keeps_deterministic_context_without_guessing() -> None:
    entry = _entry()
    service = ConsumptionAttributionService(cost_centre_enabled=True, cost_centre_provider=_FakeCostCentreProvider(None))
    result = service.attribute_entry(entry, [_resb_row()])

    assert result.requester_id == "USER01"
    assert result.order_number == "40001"
    assert result.cost_centre is None
    assert result.status is ConsumptionAttributionStatus.PARTIALLY_ATTRIBUTED


# --- D: requester only -------------------------------------------------------


def test_requester_only_leaves_order_and_cost_centre_null_and_is_partially_attributed() -> None:
    entry = _entry()
    service = ConsumptionAttributionService(cost_centre_enabled=False)
    result = service.attribute_entry(entry, [_resb_row(Aufnr=None)])

    assert result.requester_id == "USER01"
    assert result.order_number is None
    assert result.cost_centre is None
    assert result.status is ConsumptionAttributionStatus.PARTIALLY_ATTRIBUTED
    assert result.source is ConsumptionAttributionSource.RESERVATION


# --- E: no deterministic link ------------------------------------------------


def test_no_raw_reservation_row_is_unattributed_not_guessed() -> None:
    entry = _entry()
    service = ConsumptionAttributionService(cost_centre_enabled=False)
    result = service.attribute_entry(entry, [])

    assert result.requester_id is None
    assert result.order_number is None
    assert result.cost_centre is None
    assert result.status is ConsumptionAttributionStatus.UNATTRIBUTED
    assert result.source is ConsumptionAttributionSource.NONE


# --- F: multiple reservation items, same RSNUM, different RSPOS ------------


def test_multiple_items_under_same_rsnum_do_not_inherit_each_others_attribution() -> None:
    entry_1 = _entry(ledger_id="RESCHAIN-100001-0010", reservation_item="0010")
    entry_2 = _entry(ledger_id="RESCHAIN-100001-0020", reservation_item="0020")
    rows = [
        _resb_row(Rspos="0010", Wempf="USER01", Aufnr="40001"),
        _resb_row(Rspos="0020", Wempf="USER02", Aufnr=None),
    ]
    service = ConsumptionAttributionService(cost_centre_enabled=False)
    results = service.attribute_entries([entry_1, entry_2], rows)

    by_item = {r.reservation_item: r for r in results}
    assert by_item["0010"].requester_id == "USER01"
    assert by_item["0010"].order_number == "40001"
    assert by_item["0020"].requester_id == "USER02"
    assert by_item["0020"].order_number is None


# --- G: conflicting attribution ---------------------------------------------


def test_conflicting_raw_rows_for_same_key_are_reported_ambiguous_not_guessed() -> None:
    entry = _entry()
    rows = [
        _resb_row(Aufnr="40001", Wempf="USER01"),
        _resb_row(Aufnr="40002", Wempf="USER01"),
    ]
    service = ConsumptionAttributionService(cost_centre_enabled=False)
    result = service.attribute_entry(entry, rows)

    assert result.status is ConsumptionAttributionStatus.AMBIGUOUS
    assert result.requester_id is None
    assert result.order_number is None
    assert result.source is ConsumptionAttributionSource.NONE


# --- ConsumptionPlan corroboration/conflict --------------------------------


def test_consumption_plan_requester_is_used_when_resb_carries_none() -> None:
    entry = _entry()
    service = ConsumptionAttributionService(cost_centre_enabled=False)
    result = service.attribute_entry(entry, [_resb_row(Wempf=None, Aufnr=None)], _plan(requester="USER01"))

    assert result.requester_id == "USER01"
    assert result.order_number is None
    assert result.status is ConsumptionAttributionStatus.PARTIALLY_ATTRIBUTED


def test_agreeing_consumption_plan_and_resb_requester_is_attributed_not_ambiguous() -> None:
    entry = _entry()
    service = ConsumptionAttributionService(cost_centre_enabled=False)
    result = service.attribute_entry(entry, [_resb_row(Wempf="USER01")], _plan(requester="USER01"))

    assert result.requester_id == "USER01"
    assert result.status is ConsumptionAttributionStatus.ATTRIBUTED


def test_conflicting_consumption_plan_and_resb_requester_is_ambiguous() -> None:
    entry = _entry()
    service = ConsumptionAttributionService(cost_centre_enabled=False)
    result = service.attribute_entry(entry, [_resb_row(Wempf="USER01")], _plan(requester="USER99"))

    assert result.status is ConsumptionAttributionStatus.AMBIGUOUS
    assert result.requester_id is None


# --- H: idempotency (pure resolver) -----------------------------------------


def test_running_attribution_twice_on_unchanged_input_is_identical() -> None:
    entry = _entry()
    service = ConsumptionAttributionService(
        cost_centre_enabled=True, cost_centre_provider=_FakeCostCentreProvider("CC01")
    )
    first = service.attribute_entry(entry, [_resb_row()])
    second = service.attribute_entry(entry, [_resb_row()])

    assert first.status == second.status
    assert first.source == second.source
    assert first.requester_id == second.requester_id
    assert first.order_number == second.order_number
    assert first.cost_centre == second.cost_centre
    assert first.evidence == second.evidence

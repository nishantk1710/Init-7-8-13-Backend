"""Deterministic consumption attribution precedence, on ``ReservationLedgerEntry``."""

from datetime import date
from decimal import Decimal

from app.initiatives.i13.attribution import attribute_consumption
from app.initiatives.i13.models import (
    AttributionStatus,
    GiLinkStatus,
    GrLinkStatus,
    LifecycleStatus,
    ReservationLedgerEntry,
    ReservationPrLinkStatus,
)
from app.shared.material_scope import MaterialScope


def _entry(**overrides) -> ReservationLedgerEntry:
    defaults = dict(
        ledger_id="RESCHAIN-1",
        reservation_number="RS1",
        reservation_item="0001",
        material="MAT1",
        plant="1000",
        reservation_quantity=Decimal("10"),
        requirement_date=date(2026, 1, 1),
        pr_number="PR1",
        pr_item="0010",
        po_number="PO1",
        po_item="0010",
        ordered_quantity=Decimal("10"),
        received_quantity=Decimal("0"),
        issued_quantity=Decimal("0"),
        first_gr_date=None,
        last_gr_date=None,
        first_issue_date=None,
        last_issue_date=None,
        procurement_issued_quantity=Decimal("0"),
        direct_store_issued_quantity=Decimal("0"),
        lifecycle_status=LifecycleStatus.ORDERED,
        reservation_pr_link_status=ReservationPrLinkStatus.LINKED,
        gr_link_status=GrLinkStatus.NO_RECEIPTS,
        gi_link_status=GiLinkStatus.LINKED,
        gi_link_reason=None,
        material_scope=MaterialScope.OAR,
    )
    defaults.update(overrides)
    return ReservationLedgerEntry(**defaults)


def test_reservation_link_when_issued() -> None:
    entry = _entry(issued_quantity=Decimal("1"))
    result = attribute_consumption(entry)
    assert result.status is AttributionStatus.RESERVATION_LINK


def test_procurement_link_when_received_but_not_issued() -> None:
    entry = _entry(received_quantity=Decimal("10"), issued_quantity=Decimal("0"))
    result = attribute_consumption(entry)
    assert result.status is AttributionStatus.PROCUREMENT_LINK


def test_unattributed_when_nothing_resolved() -> None:
    entry = _entry(received_quantity=None)
    result = attribute_consumption(entry)
    assert result.status is AttributionStatus.UNATTRIBUTED

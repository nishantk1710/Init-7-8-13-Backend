"""Deterministic consumption attribution precedence."""

from decimal import Decimal

from app.initiatives.i13.attribution import attribute_consumption
from app.initiatives.i13.models import (
    AttributionStatus,
    LedgerUtilisationStatus,
    LinkageStatus,
    ProcurementStatus,
    UtilisationLedgerEntry,
)


def _entry(**overrides) -> UtilisationLedgerEntry:
    defaults = dict(
        ledger_id="LEDG-1",
        material="MAT1",
        plant="1000",
        reservation_number=None,
        reservation_item=None,
        pr_number="PR1",
        pr_item="0010",
        po_number="PO1",
        po_item="0010",
        received_quantity=Decimal("0"),
        issued_quantity=Decimal("0"),
        open_quantity=Decimal("0"),
        first_gr_date=None,
        latest_gr_date=None,
        first_gi_date=None,
        latest_gi_date=None,
        procurement_status=ProcurementStatus.OPEN,
        utilisation_status=LedgerUtilisationStatus.NOT_ISSUED,
        linkage_status=LinkageStatus.UNMATCHED,
        data_source="test",
    )
    defaults.update(overrides)
    return UtilisationLedgerEntry(**defaults)


def test_reservation_link_when_exact_reservation_item_matched() -> None:
    entry = _entry(reservation_number="RS1", reservation_item="0001", issued_quantity=Decimal("1"))
    result = attribute_consumption(entry)
    assert result.status is AttributionStatus.RESERVATION_LINK


def test_order_link_when_only_reservation_number_matched() -> None:
    entry = _entry(reservation_number="RS1", reservation_item=None, issued_quantity=Decimal("1"))
    result = attribute_consumption(entry)
    assert result.status is AttributionStatus.ORDER_LINK


def test_procurement_link_when_received_but_not_issued() -> None:
    entry = _entry(received_quantity=Decimal("10"), issued_quantity=Decimal("0"))
    result = attribute_consumption(entry)
    assert result.status is AttributionStatus.PROCUREMENT_LINK


def test_unattributed_when_nothing_resolved() -> None:
    entry = _entry()
    result = attribute_consumption(entry)
    assert result.status is AttributionStatus.UNATTRIBUTED

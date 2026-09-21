"""Builders for the assistant tests.

These construct **real** ``I08Assessment`` and ``I13Assessment`` objects rather
than fakes. A fake with the right attribute names would let the script keep
passing on the day an assessment changed shape, which is precisely the failure
worth catching -- the script reads these objects and nothing else checks that it
still can.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.initiatives.i13.act.domain import CrossPlantStockInfo
from app.initiatives.i13.models import AcquiredVsPlanStatus, AgingBand, WatchMetric
from app.initiatives.i13.reservation_assistant import build as build_i13
from app.initiatives.i8.register import RepairLine
from app.initiatives.i8.reservation_assistant import build as build_i08
from app.initiatives.i8.universe import UniverseRow
from app.shared.material_scope import MaterialScope

TODAY = date(2026, 7, 31)


def universe_row(**overrides) -> UniverseRow:
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
        criticality="CRITICAL",
        open_repair_lines=0,
        qty_under_repair=Decimal("0"),
        in_material_master=True,
    )
    defaults.update(overrides)
    return UniverseRow(**defaults)


def repair_line(**overrides) -> RepairLine:
    """An OPEN repair line: nothing received back."""
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


def watch_metric(**overrides) -> WatchMetric:
    defaults = dict(
        material="1000000123",
        plant="1300",
        material_scope=MaterialScope.OAR,
        stock_on_hand=Decimal("4"),
        open_po_quantity=Decimal("0"),
        average_monthly_consumption=Decimal("1"),
        months_of_cover=Decimal("4"),
        projected_months_of_cover=None,
        months_of_cover_reason=None,
        last_movement_date=date(2026, 6, 1),
        days_since_last_movement=60,
        last_issue_date=date(2026, 6, 1),
        days_since_last_issue=60,
        consumption_count_12m=12,
        consumed_qty_12m=Decimal("12"),
        inventory_turns=None,
        inventory_turns_reason=None,
        aging_band=AgingBand.FAST,
        gr_not_issued_flag=False,
        gr_not_issued_days_since_gr=None,
        gr_not_issued_relevant_gr_date=None,
        gr_not_issued_threshold_days=30,
        gr_not_issued_received_quantity=Decimal("0"),
        gr_not_issued_issued_quantity=Decimal("0"),
        gr_not_issued_outstanding_quantity=Decimal("0"),
        acquired_vs_plan_status=AcquiredVsPlanStatus.NO_PLAN,
        planned_quantity=None,
        received_quantity=Decimal("0"),
        issued_quantity=Decimal("0"),
        acquired_vs_plan_variance_quantity=None,
        acquired_vs_plan_variance_percentage=None,
        calculated_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return WatchMetric(**defaults)


def i08_assessment(*, rows=None, lines=None):
    return build_i08(
        material_id="8000005632",
        plant="1300",
        universe_rows=rows if rows is not None else [universe_row()],
        repair_lines=lines if lines is not None else [repair_line()],
        today=TODAY,
    )


def i13_assessment(*, metric=None, cross_plant=(), requested=Decimal("5")):
    return build_i13(
        material="1000000123",
        plant="1300",
        metric=metric or watch_metric(),
        cross_plant_stock=cross_plant,
        requested_quantity=requested,
    )


@pytest.fixture
def reason_categories() -> list[str]:
    return ["URGENT_BREAKDOWN", "NO_SUITABLE_REPAIRABLE", "OTHER"]


__all__ = [
    "CrossPlantStockInfo",
    "TODAY",
    "i08_assessment",
    "i13_assessment",
    "repair_line",
    "universe_row",
    "watch_metric",
]

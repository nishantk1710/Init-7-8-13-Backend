"""WATCH: backend-computed utilisation metrics per material+plant.

All calculation happens here, never inside API controllers -- routes only
call ``compute_watch_metrics`` and serialize the result.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.initiatives.i13.aging import compute_aging, group_by_material_plant
from app.initiatives.i13.config import I13Config
from app.initiatives.i13.ledger import build_utilisation_ledger
from app.initiatives.i13.models import AcquiredVsPlanStatus, UtilisationLedgerEntry, WatchMetric
from app.initiatives.i13.plans import load_consumption_plans
from app.integrations.sap.gateway import SapGateway

Row = dict[str, Any]


def _sum_stock_by_material_plant(stock_rows: list[Row]) -> dict[tuple[str, str], Decimal]:
    totals: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for row in stock_rows:
        material, plant = row.get("Matnr"), row.get("Werks")
        if material is None or plant is None:
            continue
        totals[(material, plant)] += row.get("Labst") or Decimal("0")
    return totals


def _group_ledger_by_material_plant(
    entries: list[UtilisationLedgerEntry],
) -> dict[tuple[str, str], list[UtilisationLedgerEntry]]:
    grouped: dict[tuple[str, str], list[UtilisationLedgerEntry]] = defaultdict(list)
    for entry in entries:
        grouped[(entry.material, entry.plant)].append(entry)
    return grouped


def _sum_planned_quantity(plans_by_material_plant: dict[tuple[str, str], list], key: tuple[str, str]) -> Decimal:
    return sum((plan.planned_quantity for plan in plans_by_material_plant.get(key, [])), Decimal("0"))


def _acquired_vs_plan(planned_qty: Decimal, received_qty: Decimal) -> AcquiredVsPlanStatus:
    if planned_qty <= 0:
        return AcquiredVsPlanStatus.NO_PLAN
    if received_qty < planned_qty:
        return AcquiredVsPlanStatus.BELOW_PLAN
    if received_qty > planned_qty:
        return AcquiredVsPlanStatus.ABOVE_PLAN
    return AcquiredVsPlanStatus.ON_PLAN


def _gr_not_issued(
    entries: list[UtilisationLedgerEntry], *, threshold_days: int, as_of: date
) -> tuple[bool, int | None, Decimal, Decimal, Decimal]:
    outstanding = [e for e in entries if e.open_quantity > 0 and e.latest_gr_date is not None]
    if not outstanding:
        return False, None, Decimal("0"), Decimal("0"), Decimal("0")

    ages = [(e, (as_of - e.latest_gr_date).days) for e in outstanding]
    max_age_entry, max_age = max(ages, key=lambda pair: pair[1])
    flag = max_age >= threshold_days

    received_quantity = sum((e.received_quantity for e in outstanding), Decimal("0"))
    issued_quantity = sum((e.issued_quantity for e in outstanding), Decimal("0"))
    outstanding_quantity = sum((e.open_quantity for e in outstanding), Decimal("0"))
    return flag, max_age, received_quantity, issued_quantity, outstanding_quantity


def compute_watch_metrics(
    gateway: SapGateway, config: I13Config, data_dir: Path, *, as_of: date | None = None
) -> list[WatchMetric]:
    as_of = as_of or date.today()

    movements = gateway.get_goods_movements().rows
    stock_rows = gateway.get_storage_location_stock().rows
    stock_by_key = _sum_stock_by_material_plant(stock_rows)
    movements_by_key = group_by_material_plant(movements)

    ledger_entries = build_utilisation_ledger(gateway)
    ledger_by_key = _group_ledger_by_material_plant(ledger_entries)

    plans = load_consumption_plans(data_dir)
    plans_by_key: dict[tuple[str, str], list] = defaultdict(list)
    for plan in plans:
        plans_by_key[(plan.material, plan.plant)].append(plan)

    keys = set(movements_by_key) | set(ledger_by_key)
    metrics: list[WatchMetric] = []
    for key in sorted(keys):
        material, plant = key
        current_stock = stock_by_key.get(key)
        aging = compute_aging(
            material,
            plant,
            movements_by_key.get(key, []),
            current_stock=current_stock,
            thresholds=config.aging,
            window_months=config.watch.consumption_window_months,
            as_of=as_of,
        )

        months_of_cover: Decimal | None = None
        months_of_cover_reason: str | None = None
        if aging.consumed_qty_12m > 0 and current_stock is not None:
            monthly_consumption = aging.consumed_qty_12m / Decimal(config.watch.consumption_window_months)
            months_of_cover = current_stock / monthly_consumption if monthly_consumption > 0 else None
        if months_of_cover is None:
            months_of_cover_reason = "INSUFFICIENT_HISTORY"

        entries = ledger_by_key.get(key, [])
        flag, days_since_gr, gr_received, gr_issued, gr_outstanding = _gr_not_issued(
            entries, threshold_days=config.watch.gr_not_issued_threshold_days, as_of=as_of
        )

        received_quantity = sum((e.received_quantity for e in entries), Decimal("0"))
        issued_quantity = sum((e.issued_quantity for e in entries), Decimal("0"))
        planned_quantity = _sum_planned_quantity(plans_by_key, key)

        metrics.append(
            WatchMetric(
                material=material,
                plant=plant,
                months_of_cover=months_of_cover,
                months_of_cover_reason=months_of_cover_reason,
                days_since_last_movement=aging.days_since_last_movement,
                consumption_count_12m=aging.consumption_count_12m,
                consumed_qty_12m=aging.consumed_qty_12m,
                inventory_turns=aging.inventory_turns,
                inventory_turns_reason=aging.inventory_turns_reason,
                aging_band=aging.aging_band,
                gr_not_issued_flag=flag,
                gr_not_issued_days_since_gr=days_since_gr,
                gr_not_issued_received_quantity=gr_received,
                gr_not_issued_issued_quantity=gr_issued,
                gr_not_issued_outstanding_quantity=gr_outstanding,
                acquired_vs_plan_status=_acquired_vs_plan(planned_quantity, received_quantity),
                planned_quantity=planned_quantity if planned_quantity > 0 else None,
                received_quantity=received_quantity,
                issued_quantity=issued_quantity,
            )
        )
    return metrics

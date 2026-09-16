"""WATCH: backend-computed utilisation metrics per material+plant.

All calculation happens here, never inside API controllers -- routes only
call ``compute_watch_metrics`` and serialize the result.

Postgres-backed: composes W3.5 (``movement_metrics.py``) and W6.2
(``reservation_ledger.py``) rather than reading a gateway/CSV directly.
Deliberately not OAR-scoped here (``include_out_of_scope=True`` against the
reservation ledger) -- matches this module's pre-migration behaviour, which
computed WATCH metrics for every material+plant with movement or ledger
activity; OAR scoping is applied by callers that need it (see
``exceptions.py``'s ``NO_PLAN`` check, ``summary.py``'s OAR counts).
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.initiatives.i13.config import I13Config
from app.initiatives.i13.models import AcquiredVsPlanStatus, AgingBand, ReservationLedgerEntry, WatchMetric
from app.initiatives.i13.movement_metrics import compute_all_movement_metrics
from app.initiatives.i13.plans import load_consumption_plans
from app.initiatives.i13.reservation_ledger import build_reservation_ledger
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository

Row = dict[str, Any]


def _group_ledger_by_material_plant(
    entries: list[ReservationLedgerEntry],
) -> dict[tuple[str, str], list[ReservationLedgerEntry]]:
    grouped: dict[tuple[str, str], list[ReservationLedgerEntry]] = defaultdict(list)
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
    entries: list[ReservationLedgerEntry], *, threshold_days: int, as_of: date
) -> tuple[bool, int | None, Decimal, Decimal, Decimal]:
    outstanding = [
        e
        for e in entries
        if e.last_gr_date is not None and (e.received_quantity or Decimal("0")) - e.issued_quantity > 0
    ]
    if not outstanding:
        return False, None, Decimal("0"), Decimal("0"), Decimal("0")

    ages = [(e, (as_of - e.last_gr_date).days) for e in outstanding]
    _, max_age = max(ages, key=lambda pair: pair[1])
    flag = max_age >= threshold_days

    received_quantity = sum((e.received_quantity or Decimal("0") for e in outstanding), Decimal("0"))
    issued_quantity = sum((e.issued_quantity for e in outstanding), Decimal("0"))
    outstanding_quantity = sum(
        (((e.received_quantity or Decimal("0")) - e.issued_quantity) for e in outstanding), Decimal("0")
    )
    return flag, max_age, received_quantity, issued_quantity, outstanding_quantity


def compute_watch_metrics(
    movement_repository: PostgresMovementRepository,
    procurement_repository: PostgresProcurementRepository,
    reservation_repository: PostgresReservationRepository,
    material_scope_index: dict[tuple[str, str], str | None],
    config: I13Config,
    data_dir: Path,
    *,
    material: str | None = None,
    plant: str | None = None,
    as_of: date | None = None,
) -> list[WatchMetric]:
    as_of = as_of or date.today()

    movement_metrics = compute_all_movement_metrics(
        movement_repository,
        thresholds=config.aging,
        window_months=config.watch.consumption_window_months,
        as_of=as_of,
        material=material,
        plant=plant,
    )
    movement_metrics_by_key = {(m.material, m.plant): m for m in movement_metrics}

    stock_by_key = movement_repository.get_current_stock(material=material, plant=plant)

    ledger_entries = build_reservation_ledger(
        reservation_repository,
        procurement_repository,
        material_scope_index=material_scope_index,
        material=material,
        plant=plant,
        include_out_of_scope=True,
    )
    ledger_by_key = _group_ledger_by_material_plant(ledger_entries)

    plans = load_consumption_plans(data_dir)
    plans_by_key: dict[tuple[str, str], list] = defaultdict(list)
    for plan in plans:
        plans_by_key[(plan.material, plan.plant)].append(plan)

    keys = set(movement_metrics_by_key) | set(ledger_by_key)
    metrics: list[WatchMetric] = []
    for key in sorted(keys):
        material_key, plant_key = key
        current_stock = stock_by_key.get(key)
        movement_metric = movement_metrics_by_key.get(key)

        consumed_qty_12m = movement_metric.consumption_qty_12m if movement_metric else Decimal("0")
        months_of_cover: Decimal | None = None
        months_of_cover_reason: str | None = None
        if consumed_qty_12m > 0 and current_stock is not None:
            monthly_consumption = consumed_qty_12m / Decimal(config.watch.consumption_window_months)
            months_of_cover = current_stock / monthly_consumption if monthly_consumption > 0 else None
        if months_of_cover is None:
            months_of_cover_reason = "INSUFFICIENT_HISTORY"

        entries = ledger_by_key.get(key, [])
        flag, days_since_gr, gr_received, gr_issued, gr_outstanding = _gr_not_issued(
            entries, threshold_days=config.watch.gr_not_issued_threshold_days, as_of=as_of
        )

        received_quantity = sum((e.received_quantity or Decimal("0") for e in entries), Decimal("0"))
        issued_quantity = sum((e.issued_quantity for e in entries), Decimal("0"))
        planned_quantity = _sum_planned_quantity(plans_by_key, key)

        metrics.append(
            WatchMetric(
                material=material_key,
                plant=plant_key,
                months_of_cover=months_of_cover,
                months_of_cover_reason=months_of_cover_reason,
                days_since_last_movement=movement_metric.days_since_last_movement if movement_metric else None,
                consumption_count_12m=movement_metric.consumption_count_12m if movement_metric else 0,
                consumed_qty_12m=consumed_qty_12m,
                inventory_turns=movement_metric.inventory_turns if movement_metric else None,
                inventory_turns_reason=movement_metric.inventory_turns_reason if movement_metric else "INSUFFICIENT_HISTORY",
                # No movement history at all -> NON_MOVING, the same outcome
                # compute_movement_metrics itself returns for an empty
                # movement list (classify_aging_band(None, ...)); never None.
                aging_band=movement_metric.aging_band if movement_metric else AgingBand.NON_MOVING,
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

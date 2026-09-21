"""W6.3 WATCH: backend-computed utilisation-mart metrics per material+plant --
months of cover, acquired-vs-plan, 30-day goods-received-not-issued, and the
W3.5 aging/movement classification.

All calculation happens here, never inside API controllers -- routes only
call ``compute_watch_metrics`` and serialize the result.

Postgres-backed: composes W3.5 (``movement_metrics.py``), W6.1
(``procurement_chain.py``, for open-PO quantity) and W6.2
(``reservation_ledger.py``, for acquired/issued quantities and GR-not-issued)
rather than reading a gateway/CSV directly, and never recomputes aging or
consumption itself -- see each helper below for exactly which upstream module
owns which number.

Deliberately not OAR-scoped here (``include_out_of_scope=True`` against the
reservation ledger) -- matches this module's pre-migration behaviour, which
computed WATCH metrics for every material+plant with movement or ledger
activity; OAR scoping is applied by callers that need it (see
``exceptions.py``'s ``NO_PLAN`` check, ``summary.py``'s OAR counts, and
``watch_mart.py``'s persisted mart, which defaults to OAR-only). Every
``WatchMetric`` carries its own ``material_scope`` so a caller can filter
without re-deriving the W2.4 classification.
"""

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.initiatives.i13.config import I13Config
from app.initiatives.i13.models import AcquiredVsPlanStatus, AgingBand, ReservationLedgerEntry, WatchMetric
from app.initiatives.i13.movement_metrics import compute_all_movement_metrics
from app.initiatives.i13.plans import load_consumption_plans
from app.initiatives.i13.procurement_chain import build_procurement_chain
from app.initiatives.i13.reservation_ledger import build_reservation_ledger
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.shared.material_scope import MaterialScope, classify_material_scope

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


def _acquired_vs_plan_variance(planned_qty: Decimal, received_qty: Decimal) -> tuple[Decimal | None, Decimal | None]:
    """variance_quantity/variance_percentage -- ``None`` (not zero) when
    there is no plan to vary against (planned_qty <= 0), since "no plan" and
    "exactly on plan" are different facts. No configured tolerance band
    exists anywhere in this repository's settings, so this is an exact
    comparison, per the task's explicit instruction not to invent one."""
    if planned_qty <= 0:
        return None, None
    variance_quantity = received_qty - planned_qty
    variance_percentage = (variance_quantity / planned_qty) * Decimal("100")
    return variance_quantity, variance_percentage


def _open_po_quantity_by_material_plant(
    procurement_repository: PostgresProcurementRepository, *, material: str | None, plant: str | None
) -> dict[tuple[str, str], Decimal]:
    """Open PO quantity = ordered - received, clamped at zero, summed per
    material+plant over W6.1's own procurement chain (``ordered_quantity``
    from EKPO, ``received_quantity`` net of reversals from EKBE) -- never a
    new SAP read. Clamping at zero handles a PO line where received exceeds
    ordered (over-receipt/source anomaly): that is not negative open supply.
    PR-only entries (``po_number is None``) are excluded -- nothing has been
    ordered yet, so there is no open PO quantity to count.
    """
    entries = build_procurement_chain(procurement_repository, material=material, plant=plant)
    totals: dict[tuple[str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for entry in entries:
        if entry.po_number is None or entry.ordered_quantity is None:
            continue
        open_qty = entry.ordered_quantity - entry.received_quantity
        if open_qty > 0:
            totals[(entry.material, entry.plant)] += open_qty
    return dict(totals)


def _gr_not_issued(
    entries: list[ReservationLedgerEntry], *, threshold_days: int, as_of: date
) -> tuple[bool, int | None, date | None, Decimal, Decimal, Decimal]:
    outstanding = [
        e
        for e in entries
        if e.last_gr_date is not None and (e.received_quantity or Decimal("0")) - e.issued_quantity > 0
    ]
    if not outstanding:
        return False, None, None, Decimal("0"), Decimal("0"), Decimal("0")

    ages = [(e, (as_of - e.last_gr_date).days) for e in outstanding]
    oldest_entry, max_age = max(ages, key=lambda pair: pair[1])
    flag = max_age >= threshold_days

    received_quantity = sum((e.received_quantity or Decimal("0") for e in outstanding), Decimal("0"))
    issued_quantity = sum((e.issued_quantity for e in outstanding), Decimal("0"))
    outstanding_quantity = sum(
        (((e.received_quantity or Decimal("0")) - e.issued_quantity) for e in outstanding), Decimal("0")
    )
    return flag, max_age, oldest_entry.last_gr_date, received_quantity, issued_quantity, outstanding_quantity


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
    db: Session | None = None,
) -> list[WatchMetric]:
    as_of = as_of or date.today()
    calculated_at = datetime.now(timezone.utc)

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
    open_po_by_key = _open_po_quantity_by_material_plant(procurement_repository, material=material, plant=plant)

    ledger_entries = build_reservation_ledger(
        reservation_repository,
        procurement_repository,
        material_scope_index=material_scope_index,
        material=material,
        plant=plant,
        include_out_of_scope=True,
    )
    ledger_by_key = _group_ledger_by_material_plant(ledger_entries)

    # `db` is optional and defaults to None, so a caller with no database --
    # every unit test here uses fake repositories -- gets the reference CSV
    # and nothing else, exactly as before. A caller that HAS a session passes
    # it and acquired-vs-plan starts measuring against plans real requesters
    # captured, not only the 742 the generator wrote.
    plans = load_consumption_plans(data_dir, db)
    plans_by_key: dict[tuple[str, str], list] = defaultdict(list)
    for plan in plans:
        plans_by_key[(plan.material, plan.plant)].append(plan)

    keys = set(movement_metrics_by_key) | set(ledger_by_key) | set(open_po_by_key)
    metrics: list[WatchMetric] = []
    for key in sorted(keys):
        material_key, plant_key = key
        current_stock = stock_by_key.get(key)
        movement_metric = movement_metrics_by_key.get(key)
        open_po_quantity = open_po_by_key.get(key, Decimal("0"))

        consumed_qty_12m = movement_metric.consumption_qty_12m if movement_metric else Decimal("0")
        average_monthly_consumption = consumed_qty_12m / Decimal(config.watch.consumption_window_months)

        # Zero/no consumption history must never divide into an Infinity or
        # an arbitrary large number -- see the module-level FRS note. Cover
        # is undefined (None), not "unlimited", when there is nothing to
        # divide by; current and projected share that one reason.
        months_of_cover: Decimal | None = None
        projected_months_of_cover: Decimal | None = None
        months_of_cover_reason: str | None = None
        if average_monthly_consumption > 0 and current_stock is not None:
            months_of_cover = current_stock / average_monthly_consumption
            projected_months_of_cover = (current_stock + open_po_quantity) / average_monthly_consumption
        if months_of_cover is None:
            # Kept as the pre-existing "INSUFFICIENT_HISTORY" string (already
            # part of the shipped API contract/tests) rather than switching to
            # the FRS's suggested "NO_CONSUMPTION_HISTORY" -- same meaning
            # (no usable trailing consumption, so cover is undefined), no new
            # reason vocabulary introduced for one label.
            months_of_cover_reason = "INSUFFICIENT_HISTORY"

        entries = ledger_by_key.get(key, [])
        flag, days_since_gr, relevant_gr_date, gr_received, gr_issued, gr_outstanding = _gr_not_issued(
            entries, threshold_days=config.watch.gr_not_issued_threshold_days, as_of=as_of
        )

        received_quantity = sum((e.received_quantity or Decimal("0") for e in entries), Decimal("0"))
        issued_quantity = sum((e.issued_quantity for e in entries), Decimal("0"))
        planned_quantity = _sum_planned_quantity(plans_by_key, key)
        variance_quantity, variance_percentage = _acquired_vs_plan_variance(planned_quantity, received_quantity)

        # material_scope_index only carries entries for keys a query actually
        # touched raw_marc for -- a key present only via movement/ledger
        # activity but absent from MARC (no MRP-type row) classifies EXCLUDED,
        # same as classify_material_scope's own "not configured" default.
        material_scope = classify_material_scope(material_scope_index.get(key))

        metrics.append(
            WatchMetric(
                material=material_key,
                plant=plant_key,
                material_scope=material_scope,
                stock_on_hand=current_stock,
                open_po_quantity=open_po_quantity,
                average_monthly_consumption=average_monthly_consumption,
                months_of_cover=months_of_cover,
                projected_months_of_cover=projected_months_of_cover,
                months_of_cover_reason=months_of_cover_reason,
                last_movement_date=movement_metric.last_movement_date if movement_metric else None,
                days_since_last_movement=movement_metric.days_since_last_movement if movement_metric else None,
                last_issue_date=movement_metric.last_issue_date if movement_metric else None,
                days_since_last_issue=movement_metric.days_since_last_issue if movement_metric else None,
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
                gr_not_issued_relevant_gr_date=relevant_gr_date,
                gr_not_issued_threshold_days=config.watch.gr_not_issued_threshold_days,
                gr_not_issued_received_quantity=gr_received,
                gr_not_issued_issued_quantity=gr_issued,
                gr_not_issued_outstanding_quantity=gr_outstanding,
                acquired_vs_plan_status=_acquired_vs_plan(planned_quantity, received_quantity),
                planned_quantity=planned_quantity if planned_quantity > 0 else None,
                received_quantity=received_quantity,
                issued_quantity=issued_quantity,
                acquired_vs_plan_variance_quantity=variance_quantity,
                acquired_vs_plan_variance_percentage=variance_percentage,
                calculated_at=calculated_at,
            )
        )
    return metrics

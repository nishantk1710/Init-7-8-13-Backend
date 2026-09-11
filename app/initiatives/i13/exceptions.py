"""Plan-breach, no-plan, and GR-not-issued-30-day exception engine.

Deterministic domain logic only -- no Initiative 09/10 exception types.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.initiatives.i13.config import I13Config
from app.initiatives.i13.ledger import build_utilisation_ledger
from app.initiatives.i13.models import ExceptionQueueItem, ExceptionStatus, ExceptionType, UtilisationLedgerEntry
from app.initiatives.i13.plans import ConsumptionPlan, load_consumption_plans
from app.initiatives.i13.watch import compute_watch_metrics
from app.integrations.sap.gateway import SapGateway
from app.shared.material_scope import MaterialScope, classify_material_scope

Row = dict[str, Any]


def _material_scope_index(material_plants: list[Row]) -> dict[tuple[str, str], MaterialScope]:
    return {
        (row.get("Matnr"), row.get("Werks")): classify_material_scope(row.get("Dismm"))
        for row in material_plants
        if row.get("Matnr") is not None and row.get("Werks") is not None
    }


def _index_ledger_by_reservation(
    entries: list[UtilisationLedgerEntry],
) -> dict[tuple[str, str], list[UtilisationLedgerEntry]]:
    index: dict[tuple[str, str], list[UtilisationLedgerEntry]] = defaultdict(list)
    for entry in entries:
        if entry.reservation_number and entry.reservation_item:
            index[(entry.reservation_number, entry.reservation_item)].append(entry)
    return index


def _has_issuance_evidence(entries: list[UtilisationLedgerEntry]) -> bool:
    return any(entry.issued_quantity > 0 for entry in entries)


def _plan_breach_exceptions(
    plans: list[ConsumptionPlan],
    ledger_by_reservation: dict[tuple[str, str], list[UtilisationLedgerEntry]],
    *,
    grace_days: int,
    as_of: date,
) -> list[ExceptionQueueItem]:
    exceptions: list[ExceptionQueueItem] = []
    for plan in plans:
        if plan.status != "OPEN" or plan.planned_use_date is None:
            continue
        due_date = plan.planned_use_date + timedelta(days=grace_days)
        if as_of <= due_date:
            continue

        matching_entries = ledger_by_reservation.get((plan.reservation_number, plan.reservation_item), [])
        if _has_issuance_evidence(matching_entries):
            continue

        days_overdue = (as_of - due_date).days
        exceptions.append(
            ExceptionQueueItem(
                id=f"EXC-PLAN_BREACH-{plan.plan_id}",
                type=ExceptionType.PLAN_BREACH,
                status=ExceptionStatus.OPEN,
                material=plan.material,
                plant=plan.plant,
                reservation_number=plan.reservation_number,
                pr_number=matching_entries[0].pr_number if matching_entries else None,
                po_number=matching_entries[0].po_number if matching_entries else None,
                owner_id=plan.requester,
                owner_name=None,
                created_at=datetime.now(timezone.utc),
                due_at=datetime.combine(due_date, datetime.min.time(), tzinfo=timezone.utc),
                days_overdue=days_overdue,
                reason=(
                    f"Consumption plan {plan.plan_id} planned use {plan.planned_use_date.isoformat()} "
                    f"plus {grace_days}-day grace has expired with no goods issue evidence"
                ),
                evidence=f"Rsnum {plan.reservation_number}/{plan.reservation_item}, purpose: {plan.purpose}",
            )
        )
    return exceptions


def _no_plan_exceptions(
    entries: list[UtilisationLedgerEntry],
    scope_index: dict[tuple[str, str], MaterialScope],
    planned_reservations: set[tuple[str, str]],
    *,
    as_of: date,
) -> list[ExceptionQueueItem]:
    exceptions: list[ExceptionQueueItem] = []
    for entry in entries:
        if not entry.reservation_number or not entry.reservation_item:
            continue
        if scope_index.get((entry.material, entry.plant)) is not MaterialScope.OAR:
            continue
        if (entry.reservation_number, entry.reservation_item) in planned_reservations:
            continue

        exceptions.append(
            ExceptionQueueItem(
                id=f"EXC-NO_PLAN-{entry.ledger_id}",
                type=ExceptionType.NO_PLAN,
                status=ExceptionStatus.OPEN,
                material=entry.material,
                plant=entry.plant,
                reservation_number=entry.reservation_number,
                pr_number=entry.pr_number,
                po_number=entry.po_number,
                owner_id=None,
                owner_name=None,
                created_at=datetime.now(timezone.utc),
                due_at=None,
                days_overdue=None,
                reason="OAR reservation has no matching consumption plan",
                evidence=f"Rsnum {entry.reservation_number}/{entry.reservation_item}",
            )
        )
    return exceptions


def _gr_not_issued_exceptions(gateway: SapGateway, config: I13Config, data_dir: Path, *, as_of: date) -> list[ExceptionQueueItem]:
    exceptions: list[ExceptionQueueItem] = []
    for metric in compute_watch_metrics(gateway, config, data_dir, as_of=as_of):
        if not metric.gr_not_issued_flag:
            continue
        exceptions.append(
            ExceptionQueueItem(
                id=f"EXC-GR_NOT_ISSUED_30_DAY-{metric.material}-{metric.plant}",
                type=ExceptionType.GR_NOT_ISSUED_30_DAY,
                status=ExceptionStatus.OPEN,
                material=metric.material,
                plant=metric.plant,
                reservation_number=None,
                pr_number=None,
                po_number=None,
                owner_id=None,
                owner_name=None,
                created_at=datetime.now(timezone.utc),
                due_at=None,
                days_overdue=metric.gr_not_issued_days_since_gr,
                reason=(
                    f"Goods received {metric.gr_not_issued_days_since_gr} days ago "
                    f"(>= {config.watch.gr_not_issued_threshold_days}-day threshold) with no matching issue"
                ),
                evidence=(
                    f"received={metric.gr_not_issued_received_quantity}, "
                    f"issued={metric.gr_not_issued_issued_quantity}, "
                    f"outstanding={metric.gr_not_issued_outstanding_quantity}"
                ),
            )
        )
    return exceptions


def build_exception_queue(
    gateway: SapGateway, config: I13Config, data_dir: Path, *, as_of: date | None = None
) -> list[ExceptionQueueItem]:
    as_of = as_of or date.today()

    ledger_entries = build_utilisation_ledger(gateway)
    ledger_by_reservation = _index_ledger_by_reservation(ledger_entries)
    plans = load_consumption_plans(data_dir)
    scope_index = _material_scope_index(gateway.get_material_plants().rows)
    planned_reservations = {(plan.reservation_number, plan.reservation_item) for plan in plans}

    exceptions: list[ExceptionQueueItem] = []
    exceptions.extend(
        _plan_breach_exceptions(
            plans, ledger_by_reservation, grace_days=config.exceptions.plan_breach_grace_days, as_of=as_of
        )
    )
    exceptions.extend(_no_plan_exceptions(ledger_entries, scope_index, planned_reservations, as_of=as_of))
    exceptions.extend(_gr_not_issued_exceptions(gateway, config, data_dir, as_of=as_of))
    return exceptions

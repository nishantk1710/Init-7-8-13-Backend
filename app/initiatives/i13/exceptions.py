"""Plan-breach, no-plan, and GR-not-issued-30-day exception engine.

Deterministic domain logic only -- no Initiative 09/10 exception types.

Postgres-backed: composes ``reservation_ledger.py`` (W6.2) and ``watch.py``
rather than reading a gateway/CSV directly. ``reservation_number``/
``reservation_item`` are always present on a ``ReservationLedgerEntry``
(unlike the old CSV-backed ``UtilisationLedgerEntry``, where they could be
``None``) -- this ledger is reservation-anchored by construction.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.initiatives.i13.config import I13Config
from app.initiatives.i13.models import ExceptionQueueItem, ExceptionStatus, ExceptionType, ReservationLedgerEntry
from app.initiatives.i13.plans import ConsumptionPlan, PlanMatcher, load_consumption_plans
from app.initiatives.i13.reservation_ledger import build_reservation_ledger
from app.initiatives.i13.watch import compute_watch_metrics
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.shared.material_scope import MaterialScope


def _has_issuance_evidence(entries: list[ReservationLedgerEntry]) -> bool:
    return any(entry.issued_quantity > 0 for entry in entries)


def _plan_breach_exceptions(
    plans: list[ConsumptionPlan],
    matcher: PlanMatcher,
    *,
    grace_days: int,
    as_of: date,
) -> list[ExceptionQueueItem]:
    exceptions: list[ExceptionQueueItem] = []
    for plan in plans:
        breach_from = plan.breach_reference_date
        if plan.status != "OPEN" or breach_from is None:
            continue
        due_date = breach_from + timedelta(days=grace_days)
        if as_of <= due_date:
            continue

        matching_entries = matcher.entries_for(plan)
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
                reservation_number=plan.reservation_number or None,
                pr_number=matching_entries[0].pr_number if matching_entries else None,
                po_number=matching_entries[0].po_number if matching_entries else None,
                owner_id=plan.requester,
                owner_name=None,
                created_at=datetime.now(timezone.utc),
                due_at=datetime.combine(due_date, datetime.min.time(), tzinfo=timezone.utc),
                days_overdue=days_overdue,
                reason=(
                    f"Consumption plan {plan.plan_id} planned use {breach_from.isoformat()} "
                    f"plus {grace_days}-day grace has expired with no goods issue evidence"
                ),
                evidence=(
                    f"Rsnum {plan.reservation_number}/{plan.reservation_item}, purpose: {plan.purpose}"
                    if plan.reservation_number
                    else f"Captured plan (session {plan.session_id}), purpose: {plan.purpose}"
                ),
            )
        )
    return exceptions


def _no_plan_exceptions(
    entries: list[ReservationLedgerEntry],
    matcher: PlanMatcher,
) -> list[ExceptionQueueItem]:
    exceptions: list[ExceptionQueueItem] = []
    for entry in entries:
        if entry.material_scope is not MaterialScope.OAR:
            continue
        if matcher.plan_for(entry) is not None:
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


def _gr_not_issued_exceptions(
    movement_repository: PostgresMovementRepository,
    procurement_repository: PostgresProcurementRepository,
    reservation_repository: PostgresReservationRepository,
    material_scope_index: dict[tuple[str, str], str | None],
    config: I13Config,
    data_dir: Path,
    *,
    material: str | None = None,
    plant: str | None = None,
    as_of: date,
) -> list[ExceptionQueueItem]:
    metrics = compute_watch_metrics(
        movement_repository,
        procurement_repository,
        reservation_repository,
        material_scope_index,
        config,
        data_dir,
        material=material,
        plant=plant,
        as_of=as_of,
    )
    return _grni_items(metrics, config)


def _grni_items(metrics, config: I13Config) -> list[ExceptionQueueItem]:
    exceptions: list[ExceptionQueueItem] = []
    for metric in metrics:
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
) -> list[ExceptionQueueItem]:
    """``db``, when given, adds plans captured through the assistant to the
    CSV reference plans (gap G2) -- the same optional-session convention as
    ``compute_watch_metrics``.

    ``material``/``plant`` push down into every underlying build (real SQL
    filters, not a client-side trim afterwards) -- without them, this scans
    the entire tenant's reservations/movements regardless of what an API
    caller asked for. Measured against real data: a plant-scoped call still
    takes double-digit seconds (this composes ``build_reservation_ledger``
    AND ``compute_watch_metrics``, which itself rebuilds a reservation ledger
    internally); an unfiltered call is worse. There is no caching anywhere in
    I13 today -- see the W3.5/W6.1/W6.2 implementation reports' performance
    notes for the follow-up this implies.
    """
    as_of = as_of or date.today()

    ledger_entries = build_reservation_ledger(
        reservation_repository,
        procurement_repository,
        material_scope_index=material_scope_index,
        material=material,
        plant=plant,
        include_out_of_scope=True,
    )
    plans = load_consumption_plans(data_dir, db)
    if material:
        plans = [plan for plan in plans if plan.material == material]
    if plant:
        plans = [plan for plan in plans if plan.plant == plant]

    matcher = PlanMatcher(plans, ledger_entries)
    exceptions: list[ExceptionQueueItem] = []
    exceptions.extend(
        _plan_breach_exceptions(plans, matcher, grace_days=config.exceptions.plan_breach_grace_days, as_of=as_of)
    )
    exceptions.extend(_no_plan_exceptions(ledger_entries, matcher))
    exceptions.extend(
        _gr_not_issued_exceptions(
            movement_repository,
            procurement_repository,
            reservation_repository,
            material_scope_index,
            config,
            data_dir,
            material=material,
            plant=plant,
            as_of=as_of,
        )
    )
    return exceptions


def exception_queue_from(
    ledger_entries: list[ReservationLedgerEntry],
    plans: list[ConsumptionPlan],
    watch_metrics,
    config: I13Config,
    *,
    as_of: date,
) -> list[ExceptionQueueItem]:
    """The same queue as :func:`build_exception_queue`, over components the
    caller already holds -- the I13 snapshot's reservation ledger and WATCH
    rows plus the current plans -- instead of rebuilding them.

    Identical rules, identical order (plan breaches, then no-plan, then GRNI).
    GRNI does not depend on plans, so the snapshot's WATCH rows (computed with
    captured plans) flag exactly what the plan-less recompute in
    :func:`_gr_not_issued_exceptions` would.
    """
    matcher = PlanMatcher(plans, ledger_entries)
    exceptions: list[ExceptionQueueItem] = []
    exceptions.extend(
        _plan_breach_exceptions(plans, matcher, grace_days=config.exceptions.plan_breach_grace_days, as_of=as_of)
    )
    exceptions.extend(_no_plan_exceptions(ledger_entries, matcher))
    exceptions.extend(_grni_items(watch_metrics, config))
    return exceptions

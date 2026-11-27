"""W6.6 detection, fed from the I13 snapshot when there is one.

``POST /api/i13/act/run/detect`` and the assistant's post-capture hook
(``app.assistant.turns``) both run detection through :func:`run_detection`, so
there is one way to gather its evidence:

* **ledger entries** come from the I13 snapshot's reservation ledger. With
  no snapshot (``?live=true``, snapshot disabled) the ledger is rebuilt live
  exactly as the route always did;
* **which reservation items SAP has closed** (RESB-KZEAR, for
  NOT_ISSUED_30_DAYS) from the reservation rows read for attribution anyway;
* **plans, quantity decisions, requesters** are read live, since people write
  them.

Scoped by material and/or plant, a run touches only exceptions for that scope
(``detect_exceptions`` upserts by deterministic id and never sweeps others),
which is what makes it cheap enough to run after every assistant capture.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i13.act.domain import ExceptionType, NoPlanReason
from app.initiatives.i13.act.service import (
    DetectionRunResult,
    EscalationRunResult,
    detect_exceptions,
    ReminderRunResult,
    process_escalations,
    send_requester_reminders,
    retire_no_plan_grni,
)
from app.initiatives.i13.act_exception_store import SqlExceptionRepository
from app.initiatives.i13.act_hod_provider import ConfigEscalationRecipientProvider
from app.initiatives.i13.act_requester_directory import named_requesters_by_session, parse_requester_recipients
from app.initiatives.i13.config import I13Config
from app.initiatives.i13.consumption_attribution import ConsumptionAttributionService
from app.initiatives.i13.plans import load_consumption_plans
from app.initiatives.i13.quantity_suggestion_store import (
    build_assistant_quantity_decision_records,
    build_quantity_decision_records,
)
from app.initiatives.i13.reservation_ledger import build_reservation_ledger
from app.initiatives.i13.session_link import (
    COVERED,
    load_sessions,
    session_by_reservation,
    session_status,
)
from app.initiatives.i13.snapshot import I13Snapshot, current_plans
from app.initiatives.i13.snapshot_store.reader import SqlSnapshot
from app.integrations.notifications.service import SharedNotificationPort
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import (
    PostgresReservationRepository,
    reservation_keys_since,
)


logger = get_logger(__name__)


def _notification_port(db: Session, config: I13Config) -> SharedNotificationPort:
    """The shared notification service, told which Entra user each SAP requester name stands for
    (``I13_REQUESTER_RECIPIENTS``), so the requester's "Confirm Exception" reaches their inbox."""
    return SharedNotificationPort(db, identity_aliases=parse_requester_recipients(config.escalation.requester_recipients_raw))


def run_escalations(
    db: Session,
    config: I13Config,
    *,
    as_of_time: datetime,
    limit: int | None = None,
    plants: Collection[str] | None = None,
) -> EscalationRunResult:
    """Escalate exceptions whose requester has not answered in time. The caller commits.

    Used by ``POST /api/i13/act/run/escalate`` (no ``limit``, every overdue exception) and by
    the notification scheduler (a bounded batch, only at plants that have a HOD). The HOD is told
    through the shared notification service.
    """
    return process_escalations(
        as_of_time,
        repository=SqlExceptionRepository(db),
        escalation_recipient_provider=ConfigEscalationRecipientProvider.from_config_string(
            config.escalation.hod_recipients_raw
        ),
        notification_port=_notification_port(db, config),
        limit=limit,
        plants=plants,
    )


def run_reminders(db: Session, config: I13Config, *, as_of_time: datetime) -> ReminderRunResult:
    """Remind requesters of every Planned Consumption Overdue still waiting for
    them (``service.send_requester_reminders``). The caller commits."""
    return send_requester_reminders(
        as_of_time,
        repository=SqlExceptionRepository(db),
        notification_port=_notification_port(db, config),
        every_days=config.exceptions.plan_breach_reminder_every_days,
    )


def _in_scope(material: str | None, plant: str | None):
    return lambda m, p: (not material or m == material) and (not plant or p == plant)


def _final_issued(reservation_rows) -> frozenset[tuple[str, str]]:
    """Reservation items SAP has closed for further issues (RESB-KZEAR)."""
    return frozenset((row["Rsnum"], row["Rspos"]) for row in reservation_rows if row.get("Kzear"))


def detection_types(raw: str) -> frozenset[ExceptionType] | None:
    """``I13_DETECTION_TYPES`` as exception types: comma-separated names such as
    ``PLAN_BREACH,QUANTITY_OVERRIDE``. Empty is every type (``None``). An unknown
    name is an error, not skipped: a typo that silently ran nothing would leave
    the queue stale with no sign why. ``NO_PLAN_GRNI`` (retired 5-Nov-2026) is
    dropped with a warning: there is nothing left to run for it."""
    names = [part.strip().upper() for part in (raw or "").split(",") if part.strip()]
    if not names:
        return None
    if ExceptionType.NO_PLAN_GRNI.value in names:
        logger.warning("I13_DETECTION_TYPES names NO_PLAN_GRNI, which is retired; ignoring it")
        names = [name for name in names if name != ExceptionType.NO_PLAN_GRNI.value]
        if not names:
            # Only the retired type was named: check nothing, never "every type".
            return frozenset()
    unknown = [name for name in names if name not in ExceptionType.__members__]
    if unknown:
        raise ValueError(
            f"I13_DETECTION_TYPES names unknown exception types {unknown}; "
            f"use any of {', '.join(ExceptionType.__members__)}"
        )
    return frozenset(ExceptionType[name] for name in names)


def no_plan_cutoff(raw: str) -> date | None:
    """``I13_ASSISTANT_GO_LIVE_DATE`` as a date: from when a reservation made
    without the assistant is a Non-AI exception. Empty is ``None`` -- not yet
    decided, so Non-AI raises nothing. Not the session screen's fallback (the
    first session's day): on non-prod that is weeks before any real go-live."""
    raw = (raw or "").strip()
    return date.fromisoformat(raw) if raw else None


def no_plan_eligible(
    db: Session, types: Collection[ExceptionType] | None, *, material: str | None, plant: str | None
) -> frozenset[tuple[str, str]] | None:
    """The reservation items NO_PLAN may be raised for in this run.

    Those made on or after the go-live cutoff (:func:`no_plan_cutoff`); none
    while it is unset. ``None`` only when the run does not evaluate NO_PLAN at
    all, so nothing is read for it.
    """
    if types is not None and ExceptionType.NO_PLAN not in types:
        return None
    from app.core.config import get_settings

    cutoff = no_plan_cutoff(get_settings().i13_assistant_go_live_date)
    if cutoff is None:
        logger.info("I13 detection: I13_ASSISTANT_GO_LIVE_DATE is not set, so no Non-AI exception is raised")
        return frozenset()
    keys = reservation_keys_since(db, cutoff, material=material, plant=plant)
    logger.info("I13 detection: %d reservation items made since %s can raise Non-AI", len(keys), cutoff)
    return keys


def run_detection(
    db: Session,
    config: I13Config,
    data_dir: Path,
    *,
    as_of_time: datetime,
    material: str | None = None,
    plant: str | None = None,
    snapshot: I13Snapshot | SqlSnapshot | None = None,
    types: Collection[ExceptionType] | None = None,
) -> DetectionRunResult:
    """Gather detection's evidence and run it. The caller commits.

    ``types`` limits the run to those exception types -- see
    :func:`detection_types`; ``None`` (the manual run) is every type. Non-AI
    is raised only for reservations made since the go-live cutoff
    (:func:`no_plan_eligible`), whoever runs it.
    """
    retired = retire_no_plan_grni(as_of_time=as_of_time, repository=SqlExceptionRepository(db))
    if retired:
        logger.info("I13 detection: resolved %d retired NO_PLAN_GRNI exceptions", retired)
    eligible = no_plan_eligible(db, types, material=material, plant=plant)
    if isinstance(snapshot, SqlSnapshot):
        return _run_detection_stored(
            db, config, snapshot, as_of_time=as_of_time, material=material, plant=plant, types=types,
            no_plan_eligible=eligible,
        )
    wanted = _in_scope(material, plant)
    reservation_repo = PostgresReservationRepository(db)

    if snapshot is not None:
        ledger_entries = [e for e in snapshot.reservation_ledger if wanted(e.material, e.plant)]
        plans = [p for p in current_plans(db, snapshot) if wanted(p.material, p.plant)]
        reservation_rows = reservation_repo.get_reservations(material=material, plant=plant)
    else:
        procurement_repo = PostgresProcurementRepository(db)
        material_scope_index = fetch_material_scope_index(db, material=material, plant=plant)
        ledger_entries = build_reservation_ledger(
            reservation_repo,
            procurement_repo,
            material_scope_index=material_scope_index,
            material=material,
            plant=plant,
            include_out_of_scope=True,
        )
        # `db` passed so detection sees plans CAPTURED through the assistant.
        plans = [p for p in load_consumption_plans(data_dir, db) if wanted(p.material, p.plant)]
        # Same repository instance build_reservation_ledger just used: memoized.
        reservation_rows = reservation_repo.get_reservations(material=material, plant=plant)

    # Both quantity stores: the W7.4 API's and the assistant's (gap G5).
    quantity_decision_records = build_quantity_decision_records(
        db, material=material, plant=plant
    ) + build_assistant_quantity_decision_records(
        db,
        material=material,
        plant=plant,
        # The person named in the assistant's Requester box owns the exception when I13_REQUESTER_RECIPIENTS
        # says who they are; otherwise the person who operated the assistant, as before.
        requester_aliases=parse_requester_recipients(config.escalation.requester_recipients_raw),
    )

    # W6.4's requester, so a NO_PLAN exception has somebody to route to. Only
    # resolved attributions: an AMBIGUOUS one leaves the exception unowned
    # rather than routing it to whichever name sorted first.
    attributions = ConsumptionAttributionService(
        cost_centre_enabled=config.attribution.cost_centre_enabled
    ).attribute_entries(ledger_entries, reservation_rows, plans)
    requester_by_reservation = {
        (a.reservation_number, a.reservation_item): a.requester_id for a in attributions if a.requester_id
    }

    # FR-4: which session, if any, each reservation's item text (SGTXT) names.
    no_plan_reason_by_reservation = _no_plan_reasons(db, ledger_entries, snapshot)

    # The person named in the assistant's Requester box, per session, where I13_REQUESTER_RECIPIENTS knows them.
    requester_by_session = named_requesters_by_session(
        db, parse_requester_recipients(config.escalation.requester_recipients_raw)
    )

    return detect_exceptions(
        as_of_time,
        ledger_entries=ledger_entries,
        plans=plans,
        repository=SqlExceptionRepository(db),
        notification_port=_notification_port(db, config),
        plan_breach_grace_days=config.exceptions.plan_breach_grace_days,
        requester_response_days=config.escalation.requester_response_days,
        quantity_decision_records=quantity_decision_records,
        requester_by_reservation=requester_by_reservation,
        no_plan_reason_by_reservation=no_plan_reason_by_reservation,
        requester_by_session=requester_by_session,
        types=types,
        not_issued_days=config.watch.gr_not_issued_threshold_days,
        final_issued=_final_issued(reservation_rows),
        no_plan_eligible=eligible,
    )


def _no_plan_reasons(db: Session, ledger_entries, snapshot: I13Snapshot | None) -> dict[tuple[str, str], NoPlanReason]:
    """The no-plan reason SGTXT supports for each reservation that has one.

    Without a snapshot (live path) there is no SGTXT map to hand, and the
    reason stays the plan-based default.
    """
    if snapshot is None:
        return {}
    linked = session_by_reservation(db)
    sessions = load_sessions(db, set(linked.values()))
    reasons: dict[tuple[str, str], NoPlanReason] = {}
    for entry in ledger_entries:
        key = (entry.reservation_number, entry.reservation_item)
        status = session_status(snapshot.sgtxt_by_reservation.get(key), linked.get(key), sessions)
        if status != COVERED:
            reasons[key] = NoPlanReason(status)
    return reasons


def _run_detection_stored(
    db: Session,
    config: I13Config,
    snapshot: SqlSnapshot,
    *,
    as_of_time: datetime,
    material: str | None,
    plant: str | None,
    types: Collection[ExceptionType] | None = None,
    no_plan_eligible: Collection[tuple[str, str]] | None = None,
) -> DetectionRunResult:
    """:func:`run_detection` over the SQL store, one slice of materials at a time.

    Detection only creates, updates or resolves the exceptions its evidence
    names, so slices are independent -- and a tenant-wide run never holds the
    whole ledger. A plan is evaluated in the slice holding its material; plans
    whose material has no ledger at all get a final pass, as do quantity
    decisions, which are not keyed to a ledger entry.
    """
    from app.initiatives.i13.snapshot_store.batch_repos import (
        Batch,
        BatchReservationRepository,
    )
    from app.initiatives.i13.snapshot_store.work_tables import WorkTables

    wanted = _in_scope(material, plant)
    plans = [p for p in snapshot.current_plans() if wanted(p.material, p.plant)]
    quantity_decision_records = build_quantity_decision_records(
        db, material=material, plant=plant
    ) + build_assistant_quantity_decision_records(
        db,
        material=material,
        plant=plant,
        # The person named in the assistant's Requester box owns the exception when I13_REQUESTER_RECIPIENTS
        # says who they are; otherwise the person who operated the assistant, as before.
        requester_aliases=parse_requester_recipients(config.escalation.requester_recipients_raw),
    )
    requester_by_session = named_requesters_by_session(
        db, parse_requester_recipients(config.escalation.requester_recipients_raw)
    )
    linked = session_by_reservation(db)
    sessions = load_sessions(db, set(linked.values()))
    work = WorkTables(snapshot.version)
    attributor = ConsumptionAttributionService(cost_centre_enabled=config.attribution.cost_centre_enabled)

    totals = {"created": 0, "reused": 0, "resolved": 0, "routed": 0}
    covered_plans: set[int] = set()
    ranges = snapshot.material_ranges(material=material, plant=plant)

    def detect(ledger_entries, slice_plans, reservation_rows, sgtxt, quantity_records) -> None:
        attributions = attributor.attribute_entries(ledger_entries, reservation_rows, slice_plans)
        reasons = {}
        for entry in ledger_entries:
            key = (entry.reservation_number, entry.reservation_item)
            status = session_status(sgtxt.get(key), linked.get(key), sessions)
            if status != COVERED:
                reasons[key] = NoPlanReason(status)
        result = detect_exceptions(
            as_of_time,
            ledger_entries=ledger_entries,
            plans=slice_plans,
            repository=SqlExceptionRepository(db),
            notification_port=_notification_port(db, config),
            plan_breach_grace_days=config.exceptions.plan_breach_grace_days,
            requester_response_days=config.escalation.requester_response_days,
            quantity_decision_records=quantity_records,
            requester_by_reservation={
                (a.reservation_number, a.reservation_item): a.requester_id for a in attributions if a.requester_id
            },
            no_plan_reason_by_reservation=reasons,
            requester_by_session=requester_by_session,
            types=types,
            not_issued_days=config.watch.gr_not_issued_threshold_days,
            final_issued=_final_issued(reservation_rows),
            no_plan_eligible=no_plan_eligible,
        )
        for name in totals:
            totals[name] += getattr(result, name)

    for lo, hi in ranges:
        batch = Batch(lo, hi, plant)
        rows = snapshot.ledger_in_range(lo, hi, plant=plant)
        ledger_entries = [e for e, _ in rows if wanted(e.material, e.plant)]
        sgtxt = {(e.reservation_number, e.reservation_item): t for e, t in rows if t}
        slice_plans = [p for p in plans if batch.holds(p.material, p.plant)]
        covered_plans.update(id(p) for p in slice_plans)
        reservation_rows = BatchReservationRepository(db, work, batch).get_reservations(material=material)
        detect(ledger_entries, slice_plans, reservation_rows, sgtxt, [])

    leftover = [p for p in plans if id(p) not in covered_plans]
    if leftover or quantity_decision_records:
        detect([], leftover, [], {}, quantity_decision_records)

    return DetectionRunResult(as_of_time=as_of_time, **totals)

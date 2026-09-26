"""W6.6 ACT application services: ``detect_exceptions``, ``process_escalations``
and ``submit_confirmation``.

Callable independently of any scheduler (FastAPI routes and tests both call
these directly) -- see ``app/api/i13/act.py`` for the manual/local trigger
endpoints and this package's ``__init__.py`` for why nothing here imports
SQLAlchemy, an Azure SDK or a notification-provider SDK. Every dependency is
one of the ``ports.py`` Protocols, injected by the caller.

The platform stays read-only toward SAP: nothing in this module issues
stock, transfers stock, cancels a reservation, changes OAR category/MRP
parameters, or writes back to SAP. Cross-plant stock context
(``get_cross_plant_stock``) is informational only.

Reuses W6.3's ``WatchGrniSnapshot`` (never recomputes GRNI) and W6.2's
``ReservationLedgerEntry``/``ConsumptionPlan`` (never re-stitches the
ledger).

**Dedup rule** (see ``state_machine.py``'s docstring for the state-machine
half of this): each exception type's business key is the smallest identity
that correctly names one unresolved condition --

* ``PLAN_BREACH`` -- the consumption plan's ``plan_id`` (one breach per
  plan).
* ``NO_PLAN`` / ``NO_PLAN_GRNI`` -- ``(reservation_number, reservation_item)``,
  the natural grain of a W6.2 ``ReservationLedgerEntry``.
* ``QUANTITY_OVERRIDE`` -- ``(reservation_number, reservation_item)`` when
  known, else the session id, else ``(material, plant)`` as a last resort.

A detection run is idempotent against ``ExceptionRepository.upsert`` keyed
on this id: run 1 creates, run 2 against the same still-unresolved condition
reuses the same row (and routes it if a requester has since become
resolvable), and a ``RESOLVED`` exception is never reopened by a later run
against the same key -- only a genuinely new business key (e.g. a new plan,
a new reservation) creates a new exception record.

Where the owner comes from, and why there are two sources
----------------------------------------------------------
An exception with no owner routes to nobody and escalates to nobody, so
FR-9's whole second half is dead for it. The owner used to come from
``ConsumptionPlan.requester`` alone -- and a ``NO_PLAN`` exception, by
definition, has no plan. Every one of them was therefore unowned, which is
how 42,649 exceptions came to sit unrouted.

So the owner is resolved in two steps, in descending order of authority:

1. ``plan.requester`` -- the person who actually spoke to the assistant.
   Unchanged, and still preferred wherever it exists.
2. ``requester_by_reservation`` -- W6.4's attribution, resolved from
   ``RESB.WEMPF`` for roughly four reservations in five.

The second is passed in already resolved, exactly like ``grni_snapshots``:
this function still does no I/O of its own. W6.4's conflict rule travels with
it -- where RESB and the plan name different people, it yields *no* entry
rather than a guess, so an AMBIGUOUS attribution leaves the exception unowned.
That is deliberate. An exception routed to the wrong named person is worse
than one routed to nobody, because somebody answers it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.initiatives.i13.act.detection import (
    build_exception_id,
    classify_no_plan_reason,
    detect_plan_breach,
    detect_quantity_override,
    no_plan_grni_applies,
    quantity_override_business_key,
)
from app.initiatives.i13.act.domain import (
    NoPlanReason,
    ActException,
    AssigneeType,
    CrossPlantStockInfo,
    EventType,
    ExceptionEvent,
    ExceptionStatus,
    ExceptionType,
    NotificationAttempt,
    NotificationChannel,
    NotificationIntent,
    NotificationOutcome,
    NotificationResult,
    QuantityDecisionRecord,
    RequesterConfirmation,
    RoutingStatus,
    WatchGrniSnapshot,
)
from app.initiatives.i13.act.ports import (
    CrossPlantStockProvider,
    EscalationRecipientProvider,
    ExceptionRepository,
    NotificationPort,
)
from app.initiatives.i13.act.state_machine import validate_transition
from app.initiatives.i13.models import ReservationLedgerEntry
from app.initiatives.i13.plans import ConsumptionPlan, PlanMatcher
from app.shared.material_scope import MaterialScope

_AUTO_RESOLVABLE_STATUSES = frozenset(
    {ExceptionStatus.OPEN, ExceptionStatus.AWAITING_REQUESTER, ExceptionStatus.ESCALATED}
)


@dataclass(frozen=True)
class DetectionRunResult:
    as_of_time: datetime
    created: int
    reused: int
    resolved: int
    routed: int


@dataclass(frozen=True)
class EscalationRunResult:
    as_of_time: datetime
    escalated: int
    routing_pending: int


def _transition(
    exception: ActException,
    to_status: ExceptionStatus,
    *,
    repository: ExceptionRepository,
    as_of_time: datetime,
    actor_id: str | None,
    actor_type: str,
    event_type: EventType,
    metadata: dict[str, str] | None = None,
    **field_updates: object,
) -> ActException:
    validate_transition(exception.status, to_status)
    updated = dataclasses.replace(exception, status=to_status, updated_at=as_of_time, **field_updates)
    repository.upsert(updated)
    repository.append_event(
        ExceptionEvent(
            exception_id=exception.exception_id,
            event_type=event_type,
            from_status=exception.status,
            to_status=to_status,
            actor_id=actor_id,
            actor_type=actor_type,
            timestamp=as_of_time,
            metadata=metadata or {},
        )
    )
    return updated


def _notify(
    repository: ExceptionRepository,
    notification_port: NotificationPort,
    exception: ActException,
    channel: NotificationChannel,
    recipient: str | None,
    *,
    subject: str,
    body: str,
    as_of_time: datetime,
) -> None:
    """Notification delivery never corrupts the exception's already-persisted
    state transition: the transition happens (and is committed to the
    repository) before this is called, and any exception raised by the
    notification adapter itself is caught and recorded as a failed attempt
    rather than propagated."""
    intent = NotificationIntent(exception_id=exception.exception_id, channel=channel, recipient=recipient, subject=subject, body=body)
    try:
        result = notification_port.send(intent)
    except Exception as exc:  # adapter failure must not corrupt exception state
        result = NotificationResult(outcome=NotificationOutcome.FAILED, detail=f"notification adapter raised: {exc}")

    repository.record_notification(
        NotificationAttempt(
            exception_id=exception.exception_id,
            channel=channel,
            recipient=recipient,
            outcome=result.outcome,
            detail=result.detail,
            attempted_at=as_of_time,
        )
    )
    event_type = EventType.NOTIFICATION_FAILED if result.outcome == NotificationOutcome.FAILED else EventType.NOTIFICATION_SENT
    repository.append_event(
        ExceptionEvent(
            exception_id=exception.exception_id,
            event_type=event_type,
            from_status=exception.status,
            to_status=exception.status,
            actor_id=None,
            actor_type="SYSTEM",
            timestamp=as_of_time,
            metadata={"channel": channel.value, "recipient": recipient or "", "outcome": result.outcome.value, "detail": result.detail},
        )
    )


def _route_to_requester(
    exception: ActException,
    requester_id: str,
    response_period: timedelta,
    as_of_time: datetime,
    repository: ExceptionRepository,
    notification_port: NotificationPort,
) -> ActException:
    updated = _transition(
        exception,
        ExceptionStatus.AWAITING_REQUESTER,
        repository=repository,
        as_of_time=as_of_time,
        actor_id=None,
        actor_type="SYSTEM",
        event_type=EventType.ROUTED_TO_REQUESTER,
        metadata={"requester_id": requester_id},
        owner_requester_id=requester_id,
        requester_due_at=as_of_time + response_period,
        current_assignee_type=AssigneeType.REQUESTER,
        current_assignee_id=requester_id,
        routing_status=RoutingStatus.RESOLVED,
    )
    subject = f"ACT exception {updated.exception_id} requires your confirmation"
    _notify(repository, notification_port, updated, NotificationChannel.PLATFORM_QUEUE, requester_id, subject=subject, body=updated.reason, as_of_time=as_of_time)
    _notify(repository, notification_port, updated, NotificationChannel.EMAIL, requester_id, subject=subject, body=updated.reason, as_of_time=as_of_time)
    return updated


def _apply_detection(
    *,
    exception_id: str,
    condition_holds: bool,
    exception_type: ExceptionType,
    material: str,
    plant: str,
    reservation_number: str | None,
    reservation_item: str | None,
    session_id: str | None,
    ledger_entry_id: str | None,
    owner_requester_id: str | None,
    reason: str,
    evidence: dict[str, str],
    as_of_time: datetime,
    response_period: timedelta,
    repository: ExceptionRepository,
    notification_port: NotificationPort,
) -> tuple[int, int, int, int]:
    """One exception type's evaluation against one business key. Returns
    ``(created, reused, resolved, routed)`` counts. See this module's
    docstring for the dedup/reopen rule this implements."""
    existing = repository.get(exception_id)

    if not condition_holds:
        if existing is not None and existing.status in _AUTO_RESOLVABLE_STATUSES:
            _transition(
                existing,
                ExceptionStatus.RESOLVED,
                repository=repository,
                as_of_time=as_of_time,
                actor_id=None,
                actor_type="SYSTEM",
                event_type=EventType.RESOLVED,
                metadata={"reason": "condition no longer applies"},
                resolved_at=as_of_time,
            )
            return (0, 0, 1, 0)
        return (0, 0, 0, 0)

    if existing is None:
        new_exception = ActException(
            exception_id=exception_id,
            exception_type=exception_type,
            status=ExceptionStatus.OPEN,
            material=material,
            plant=plant,
            reservation_number=reservation_number,
            reservation_item=reservation_item,
            session_id=session_id,
            ledger_entry_id=ledger_entry_id,
            owner_requester_id=owner_requester_id,
            detected_at=as_of_time,
            requester_due_at=None,
            escalated_at=None,
            resolved_at=None,
            current_assignee_type=None,
            current_assignee_id=None,
            routing_status=None,
            reason=reason,
            evidence=evidence,
            created_at=as_of_time,
            updated_at=as_of_time,
        )
        repository.upsert(new_exception)
        repository.append_event(
            ExceptionEvent(
                exception_id=exception_id,
                event_type=EventType.DETECTED,
                from_status=None,
                to_status=ExceptionStatus.OPEN,
                actor_id=None,
                actor_type="SYSTEM",
                timestamp=as_of_time,
                metadata={"reason": reason},
            )
        )
        routed = 0
        if owner_requester_id:
            _route_to_requester(new_exception, owner_requester_id, response_period, as_of_time, repository, notification_port)
            routed = 1
        return (1, 0, 0, routed)

    if existing.status == ExceptionStatus.RESOLVED:
        # Dedup rule: never reopen a resolved exception for the same
        # business key.
        return (0, 1, 0, 0)

    routed = 0
    if existing.status is ExceptionStatus.OPEN and owner_requester_id and not existing.owner_requester_id:
        with_owner = dataclasses.replace(existing, owner_requester_id=owner_requester_id)
        _route_to_requester(with_owner, owner_requester_id, response_period, as_of_time, repository, notification_port)
        routed = 1
    return (0, 1, 0, routed)


def detect_exceptions(
    as_of_time: datetime,
    *,
    ledger_entries: Sequence[ReservationLedgerEntry],
    plans: Sequence[ConsumptionPlan],
    grni_snapshots: Mapping[tuple[str, str], WatchGrniSnapshot],
    repository: ExceptionRepository,
    notification_port: NotificationPort,
    plan_breach_grace_days: int,
    requester_response_days: int,
    quantity_decision_records: Sequence[QuantityDecisionRecord] = (),
    requester_by_reservation: Mapping[tuple[str, str], str] = {},
    no_plan_reason_by_reservation: Mapping[tuple[str, str], NoPlanReason] = {},
) -> DetectionRunResult:
    """Run PLAN_BREACH, NO_PLAN/NO_PLAN_GRNI and QUANTITY_OVERRIDE detection
    for the given already-fetched evidence, and persist/update/resolve
    exceptions accordingly. Fetching ``ledger_entries``/``plans``/
    ``grni_snapshots``/``requester_by_reservation`` (from W6.2/W6.3/W6.4/
    CAPTURE) is the caller's job -- this function does no I/O of its own
    beyond the injected ports, so it is trivially unit-testable and callable
    from a route, a script, or a future scheduler without any change.

    ``requester_by_reservation`` maps ``(reservation_number,
    reservation_item)`` to the requester W6.4 resolved from ``RESB.WEMPF``.
    Defaulting it to empty keeps every existing caller and test working
    unchanged, and reproduces exactly the old behaviour: plan requester only.
    """
    grace_period = timedelta(days=plan_breach_grace_days)
    response_period = timedelta(days=requester_response_days)
    created = reused = resolved = routed = 0

    def owner_for(
        plan: ConsumptionPlan | None,
        reservation_number: str | None,
        reservation_item: str | None,
    ) -> str | None:
        """The requester to route to, preferring the one who spoke to us.

        Returns ``None`` when neither source knows, which is a real answer:
        the exception is persisted unowned and routed by a later run once a
        requester becomes resolvable (``_apply_detection`` handles that), and
        never routed to a guess in the meantime.
        """
        if plan is not None and plan.requester:
            return plan.requester
        if reservation_number is None or reservation_item is None:
            return None
        return requester_by_reservation.get((reservation_number, reservation_item))

    # Reference plans match reservations by number; captured plans not yet
    # linked to one (FR-8) match by material, plant and window -- see
    # PlanMatcher. Keying every plan by reservation number, as this did,
    # collided all unlinked captured plans on ("", "") (gaps G3/G4).
    matcher = PlanMatcher(list(plans), list(ledger_entries))

    for plan in plans:
        entries = matcher.entries_for(plan)
        breached = detect_plan_breach(plan, entries, as_of_time=as_of_time, grace_period=grace_period)
        c, r, res, rt = _apply_detection(
            exception_id=build_exception_id("PLAN_BREACH", plan.plan_id),
            condition_holds=breached,
            exception_type=ExceptionType.PLAN_BREACH,
            material=plan.material,
            plant=plan.plant,
            # An unlinked captured plan has no reservation yet; "" is not one.
            reservation_number=plan.reservation_number or None,
            reservation_item=plan.reservation_item or None,
            session_id=plan.session_id,
            ledger_entry_id=entries[0].ledger_id if entries else None,
            # A plan breach always has a plan, so this is almost always
            # plan.requester. The fallback covers a plan captured with no
            # requester recorded against it.
            owner_requester_id=owner_for(plan, plan.reservation_number, plan.reservation_item),
            reason=(
                f"Consumption plan {plan.plan_id} planned use {plan.breach_reference_date} plus "
                f"{plan_breach_grace_days}-day grace has expired with no goods issue evidence"
            ),
            evidence={"plan_id": plan.plan_id, "planned_use_date": str(plan.breach_reference_date)},
            as_of_time=as_of_time,
            response_period=response_period,
            repository=repository,
            notification_port=notification_port,
        )
        created += c
        reused += r
        resolved += res
        routed += rt

    for entry in ledger_entries:
        if entry.material_scope is not MaterialScope.OAR:
            continue
        plan = matcher.plan_for(entry)
        no_plan_reason = classify_no_plan_reason(plan)
        if plan is None:
            # What the reservation's item text (SGTXT) says, when known: a
            # mistyped or foreign session ID, or a real session that never
            # captured a plan, is a different finding from no ID at all.
            no_plan_reason = no_plan_reason_by_reservation.get(
                (entry.reservation_number, entry.reservation_item), no_plan_reason
            )
        grni_snapshot = grni_snapshots.get((entry.material, entry.plant))
        grni_flag = grni_snapshot.gr_not_issued_flag if grni_snapshot else None

        evidence: dict[str, str] = {
            "reservation_number": entry.reservation_number,
            "reservation_item": entry.reservation_item,
        }
        if no_plan_reason is not None:
            evidence["no_plan_reason"] = no_plan_reason.value

        c, r, res, rt = _apply_detection(
            exception_id=build_exception_id("NO_PLAN", entry.reservation_number, entry.reservation_item),
            condition_holds=no_plan_reason is not None,
            exception_type=ExceptionType.NO_PLAN,
            material=entry.material,
            plant=entry.plant,
            reservation_number=entry.reservation_number,
            reservation_item=entry.reservation_item,
            session_id=plan.session_id if plan else None,
            ledger_entry_id=entry.ledger_id,
            # THE case this fallback exists for: a NO_PLAN exception has no
            # plan by definition, so before W6.4 was consulted here the owner
            # was unconditionally None and nothing ever routed.
            owner_requester_id=owner_for(plan, entry.reservation_number, entry.reservation_item),
            reason=f"OAR reservation has no valid plan/session ({no_plan_reason.value if no_plan_reason else 'n/a'})",
            evidence=evidence,
            as_of_time=as_of_time,
            response_period=response_period,
            repository=repository,
            notification_port=notification_port,
        )
        created += c
        reused += r
        resolved += res
        routed += rt

        grni_evidence = dict(evidence)
        if grni_snapshot is not None:
            grni_evidence["days_since_gr"] = str(grni_snapshot.gr_not_issued_days_since_gr)
            grni_evidence["threshold_days"] = str(grni_snapshot.gr_not_issued_threshold_days)
        else:
            grni_evidence["watch_mart_status"] = "SOURCE_UNAVAILABLE"

        c, r, res, rt = _apply_detection(
            exception_id=build_exception_id("NO_PLAN_GRNI", entry.reservation_number, entry.reservation_item),
            condition_holds=no_plan_grni_applies(no_plan_reason, grni_flag),
            exception_type=ExceptionType.NO_PLAN_GRNI,
            material=entry.material,
            plant=entry.plant,
            reservation_number=entry.reservation_number,
            reservation_item=entry.reservation_item,
            session_id=plan.session_id if plan else None,
            ledger_entry_id=entry.ledger_id,
            # Same reasoning as NO_PLAN above -- this is its GRNI sibling.
            owner_requester_id=owner_for(plan, entry.reservation_number, entry.reservation_item),
            reason="No valid plan/session and W6.3's GRNI threshold is exceeded (reused from the WATCH mart, not recalculated)",
            evidence=grni_evidence,
            as_of_time=as_of_time,
            response_period=response_period,
            repository=repository,
            notification_port=notification_port,
        )
        created += c
        reused += r
        resolved += res
        routed += rt

    for record in quantity_decision_records:
        evaluation = detect_quantity_override(record)
        evidence = {
            "requested_quantity": str(record.requested_quantity),
            "suggested_quantity": (str(record.suggested_quantity) if record.suggested_quantity is not None else "SOURCE_UNAVAILABLE"),
            "variance": str(evaluation.variance) if evaluation.variance is not None else "NOT_APPLICABLE",
        }
        if record.suggestion_reason:
            evidence["suggestion_reason"] = record.suggestion_reason
        if record.override_justification:
            evidence["override_justification"] = record.override_justification

        c, r, res, rt = _apply_detection(
            exception_id=build_exception_id("QUANTITY_OVERRIDE", quantity_override_business_key(record)),
            condition_holds=evaluation.available and evaluation.override,
            exception_type=ExceptionType.QUANTITY_OVERRIDE,
            material=record.material,
            plant=record.plant,
            reservation_number=record.reservation_number,
            reservation_item=record.reservation_item,
            session_id=record.session_id,
            ledger_entry_id=None,
            # W7.4 already carries the requester on the decision record -- the
            # person who chose the quantity. Falls back to W6.4 only where the
            # suggestion was recorded without one.
            owner_requester_id=(
                record.requester_id
                or owner_for(None, record.reservation_number, record.reservation_item)
            ),
            reason="Requester-confirmed quantity differs from the system suggestion",
            evidence=evidence,
            as_of_time=as_of_time,
            response_period=response_period,
            repository=repository,
            notification_port=notification_port,
        )
        created += c
        reused += r
        resolved += res
        routed += rt

    return DetectionRunResult(as_of_time=as_of_time, created=created, reused=reused, resolved=resolved, routed=routed)


def process_escalations(
    as_of_time: datetime,
    *,
    repository: ExceptionRepository,
    escalation_recipient_provider: EscalationRecipientProvider,
    notification_port: NotificationPort,
) -> EscalationRunResult:
    """Escalate every ``AWAITING_REQUESTER`` exception whose
    ``requester_due_at`` has passed. The exception's status moves to
    ``ESCALATED`` either way -- that is the workflow milestone ("this is now
    overdue and needs HOD attention"); whether a real HOD identity was found
    is tracked separately in ``routing_status``/``current_assignee_id`` so a
    routing failure is visible without ever inventing a recipient (FRS
    §11/W6.6 instruction: never fabricate a routing target)."""
    escalated = routing_pending = 0
    due = [
        exc
        for exc in repository.list(status=ExceptionStatus.AWAITING_REQUESTER)
        if exc.requester_due_at is not None and as_of_time >= exc.requester_due_at
    ]
    for exception in due:
        hod_id = escalation_recipient_provider.get_hod(
            material=exception.material, plant=exception.plant, requester_id=exception.owner_requester_id
        )
        if hod_id:
            updated = _transition(
                exception,
                ExceptionStatus.ESCALATED,
                repository=repository,
                as_of_time=as_of_time,
                actor_id=None,
                actor_type="SYSTEM",
                event_type=EventType.ESCALATED_TO_HOD,
                metadata={"hod_id": hod_id},
                escalated_at=as_of_time,
                current_assignee_type=AssigneeType.HOD,
                current_assignee_id=hod_id,
                routing_status=RoutingStatus.RESOLVED,
            )
            subject = f"ACT exception {updated.exception_id} escalated for your review"
            _notify(repository, notification_port, updated, NotificationChannel.PLATFORM_QUEUE, hod_id, subject=subject, body=updated.reason, as_of_time=as_of_time)
            _notify(repository, notification_port, updated, NotificationChannel.EMAIL, hod_id, subject=subject, body=updated.reason, as_of_time=as_of_time)
            escalated += 1
        else:
            _transition(
                exception,
                ExceptionStatus.ESCALATED,
                repository=repository,
                as_of_time=as_of_time,
                actor_id=None,
                actor_type="SYSTEM",
                event_type=EventType.ROUTING_FAILED,
                metadata={"detail": "no HOD could be resolved for this material/plant"},
                escalated_at=as_of_time,
                current_assignee_type=None,
                current_assignee_id=None,
                routing_status=RoutingStatus.PENDING,
            )
            routing_pending += 1
    return EscalationRunResult(as_of_time=as_of_time, escalated=escalated, routing_pending=routing_pending)


def submit_confirmation(
    *,
    exception_id: str,
    reason_category: str,
    free_text: str,
    actor_id: str,
    as_of_time: datetime,
    repository: ExceptionRepository,
) -> ActException:
    """Requester confirmation + structured justification. Only valid from
    ``AWAITING_REQUESTER`` -- ``state_machine.validate_transition`` (via
    ``_transition``) rejects anything else, including an exception that was
    never routed (``OPEN``) or already resolved."""
    exception = repository.get(exception_id)
    if exception is None:
        raise LookupError(f"no ACT exception {exception_id!r}")

    confirmation = RequesterConfirmation(
        exception_id=exception_id,
        reason_category=reason_category,
        free_text=free_text,
        actor_id=actor_id,
        submitted_at=as_of_time,
    )
    updated = _transition(
        exception,
        ExceptionStatus.CONFIRMED,
        repository=repository,
        as_of_time=as_of_time,
        actor_id=actor_id,
        actor_type="REQUESTER",
        event_type=EventType.REQUESTER_CONFIRMED,
        metadata={"reason_category": reason_category},
    )
    # The confirmation itself, and its JUSTIFICATION_ADDED audit event, are
    # only persisted once the transition above has validated and succeeded --
    # an invalid transition raises before either is written.
    repository.save_confirmation(confirmation)
    repository.append_event(
        ExceptionEvent(
            exception_id=exception_id,
            event_type=EventType.JUSTIFICATION_ADDED,
            from_status=exception.status,
            to_status=updated.status,
            actor_id=actor_id,
            actor_type="REQUESTER",
            timestamp=as_of_time,
            metadata={"reason_category": reason_category},
        )
    )
    return updated


def get_cross_plant_stock(exception: ActException, provider: CrossPlantStockProvider) -> list[CrossPlantStockInfo]:
    """Informational-only cross-plant stock context for one exception. Never
    used to create a reservation or transfer at another plant -- see this
    module's docstring."""
    return provider.get_other_plant_stock(material=exception.material, exclude_plant=exception.plant)

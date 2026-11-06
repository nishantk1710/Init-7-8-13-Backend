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

Reuses W6.2's ``ReservationLedgerEntry``/``ConsumptionPlan`` (never
re-stitches the ledger).

**Dedup rule** (see ``state_machine.py``'s docstring for the state-machine
half of this): each exception type's business key is the smallest identity
that correctly names one unresolved condition --

* ``PLAN_BREACH`` -- the consumption plan's ``plan_id`` (one breach per
  plan).
* ``NO_PLAN`` / ``NOT_ISSUED_30_DAYS`` -- ``(reservation_number,
  reservation_item)``, the natural grain of a W6.2 ``ReservationLedgerEntry``.
  (``NO_PLAN_GRNI`` used the same key; retired 5-Nov-2026,
  :func:`retire_no_plan_grni`.)
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

The second is passed in already resolved: this function still does no I/O
of its own. W6.4's conflict rule travels with
it -- where RESB and the plan name different people, it yields *no* entry
rather than a guess, so an AMBIGUOUS attribution leaves the exception unowned.
That is deliberate. An exception routed to the wrong named person is worse
than one routed to nobody, because somebody answers it.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

from app.initiatives.i13.act.detection import (
    build_exception_id,
    classify_no_plan_reason,
    detect_plan_breach,
    NotIssuedBasis,
    detect_not_issued,
    detect_quantity_override,
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
)
from app.initiatives.i13.act.ports import (
    CrossPlantStockProvider,
    EscalationRecipientProvider,
    ExceptionRepository,
    NotificationPort,
)
from app.initiatives.i13.act.state_machine import validate_transition
from app.integrations.notifications.templates import EXCEPTION_ESCALATED, EXCEPTION_RESPONSE_REQUIRED
from app.initiatives.i13.models import ReservationLedgerEntry
from app.initiatives.i13.plans import ConsumptionPlan, PlanMatcher, PlanSource
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


#: The frontend page that lists these exceptions; notifications link to it.
EXCEPTION_PAGE = "/oar-utilization/aging-exceptions"


#: What the requester is asked, word for word, in the "Confirm Exception" notification.
REQUESTER_QUESTION = "Why was this material not consumed as planned, in your own words."


def exception_link(exception: ActException) -> str:
    """The Procurement Exceptions page narrowed to this exception's material and plant.

    The page filters on exactly these two query parameters, so the link opens the material's own
    exceptions instead of the whole list.
    """
    return f"{EXCEPTION_PAGE}?material={quote(exception.material, safe='')}&plant={quote(exception.plant, safe='')}"


def _details(exception: ActException) -> list[str]:
    """The lines both messages share: what the exception is, in business terms. ``Material:`` stays a
    ``Label: value`` line, which is how the inbox picks the material out as the reference."""
    return [
        f"Material: {exception.material}",
        f"Plant: {exception.plant}",
        f"Exception: {exception.reason or '-'}",
        f"Exception code: {exception.exception_type.value}",
    ]


def _requester_message(exception: ActException) -> str:
    """Asked of the person who requested the reservation: confirm, and say why."""
    return "\n".join([*_details(exception), REQUESTER_QUESTION])


def _requester_name(exception: ActException) -> str | None:
    """The requester as a person would read it. An Entra object id is not a name, so it is left out."""
    who = (exception.owner_requester_id or "").strip()
    if not who:
        return None
    try:
        uuid.UUID(who)
    except ValueError:
        return who
    return None


def _hod_message(exception: ActException, as_of_time: datetime) -> str:
    """Told to the HOD once the requester's time is up: still pending, and the requester has not responded."""
    waited = max(0, (as_of_time - exception.detected_at).days)
    lines = [
        "This exception is still pending: the requester has not confirmed it or given an explanation.",
        *_details(exception),
    ]
    requester = _requester_name(exception)
    if requester:
        lines.append(f"Requester: {requester}")
    lines.append(f"Detected: {exception.detected_at:%d %b %Y} ({waited} {'day' if waited == 1 else 'days'} ago)")
    lines.append("Action required: please follow up with the requester.")
    return "\n".join(lines)


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
    event_type: str | None = None,
) -> None:
    """Notification delivery never corrupts the exception's already-persisted
    state transition: the transition happens (and is committed to the
    repository) before this is called, and any exception raised by the
    notification adapter itself is caught and recorded as a failed attempt
    rather than propagated."""
    intent = NotificationIntent(
        exception_id=exception.exception_id,
        channel=channel,
        recipient=recipient,
        subject=subject,
        body=body,
        plant=exception.plant,
        entity_type="EXCEPTION",
        module="I13",
        event_type=event_type,
        action_path=exception_link(exception),
    )
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
    subject = "Confirm Exception"
    body = _requester_message(updated)
    for channel in (NotificationChannel.PLATFORM_QUEUE, NotificationChannel.EMAIL):
        _notify(repository, notification_port, updated, channel, requester_id, subject=subject, body=body, as_of_time=as_of_time, event_type=EXCEPTION_RESPONSE_REQUIRED)
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
    route: bool = True,
) -> tuple[int, int, int, int]:
    """One exception type's evaluation against one business key. Returns
    ``(created, reused, resolved, routed)`` counts. See this module's
    docstring for the dedup/reopen rule this implements.

    ``route=False`` records the exception without sending it to its owner:
    nobody is asked, notified or given a due date (QUANTITY_OVERRIDE, whose
    reason is already on file -- see :func:`_confirm_recorded_override`)."""
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
        if owner_requester_id and route:
            _route_to_requester(new_exception, owner_requester_id, response_period, as_of_time, repository, notification_port)
            routed = 1
        return (1, 0, 0, routed)

    if existing.status == ExceptionStatus.RESOLVED:
        # Dedup rule: never reopen a resolved exception for the same
        # business key.
        return (0, 1, 0, 0)

    routed = 0
    if route and existing.status is ExceptionStatus.OPEN and owner_requester_id and not existing.owner_requester_id:
        with_owner = dataclasses.replace(existing, owner_requester_id=owner_requester_id)
        _route_to_requester(with_owner, owner_requester_id, response_period, as_of_time, repository, notification_port)
        routed = 1
    return (0, 1, 0, routed)


def _confirm_recorded_override(
    exception_id: str,
    record: QuantityDecisionRecord,
    *,
    as_of_time: datetime,
    repository: ExceptionRepository,
) -> bool:
    """Confirm a QUANTITY_OVERRIDE with the reason recorded with the decision.

    The override is shown on the Exceptions screen so it can be counted, but
    there is nothing to ask anyone: the reason given in the assistant *is* the
    requester's confirmation. The state machine only confirms from
    AWAITING_REQUESTER, so an OPEN one is first assigned to its owner --
    audited, but with no notification and no due date, so it can never be
    escalated -- and confirmed in the same run. One already waiting (raised
    before 4-Nov-2026, when overrides were still sent out) is confirmed as it
    stands. Without a recorded reason it stays as it is. Returns whether it
    confirmed one.
    """
    if not record.override_reason_category:
        return False
    exception = repository.get(exception_id)
    if exception is None or exception.status not in (ExceptionStatus.OPEN, ExceptionStatus.AWAITING_REQUESTER):
        return False
    owner = exception.owner_requester_id or record.requester_id
    if exception.status is ExceptionStatus.OPEN:
        _transition(
            exception,
            ExceptionStatus.AWAITING_REQUESTER,
            repository=repository,
            as_of_time=as_of_time,
            actor_id=None,
            actor_type="SYSTEM",
            event_type=EventType.ROUTED_TO_REQUESTER,
            metadata={"requester_id": owner or "", "notified": "false", "note": "reason already recorded in the assistant"},
            owner_requester_id=owner,
            requester_due_at=None,
            current_assignee_type=AssigneeType.REQUESTER if owner else None,
            current_assignee_id=owner,
            routing_status=RoutingStatus.RESOLVED if owner else None,
        )
    submit_confirmation(
        exception_id=exception_id,
        reason_category=record.override_reason_category,
        free_text=record.override_free_text or "",
        actor_id=owner or "ASSISTANT",
        as_of_time=as_of_time,
        repository=repository,
    )
    return True


def _detect_not_issued(
    ledger_entries: Sequence[ReservationLedgerEntry],
    matcher: PlanMatcher,
    *,
    as_of_time: datetime,
    threshold_days: int,
    final_issued: Collection[tuple[str, str]],
    owner_for,
    response_period: timedelta,
    repository: ExceptionRepository,
    notification_port: NotificationPort,
) -> tuple[int, int, int, int]:
    """NOT_ISSUED_30_DAYS, one per reservation item of an assistant request line.

    Only an OAR reservation whose item text links it to an assistant plan
    (``PlanMatcher``: the R-ID or S-ID, nothing else) -- a Non-AI reservation
    is NO_PLAN's, and its unused stock shows on the 30-day GR-not-issued tab.
    Items already holding one are evaluated too, so an exception whose link
    has since gone (the R-ID removed, the material no longer OAR) is resolved
    rather than left open.
    """
    by_item: dict[tuple[str, str], list[ReservationLedgerEntry]] = {}
    for entry in ledger_entries:
        by_item.setdefault((entry.reservation_number, entry.reservation_item), []).append(entry)
    held = {
        (exception.reservation_number, exception.reservation_item)
        for exception in repository.list(exception_type=ExceptionType.NOT_ISSUED_30_DAYS)
        if exception.status is not ExceptionStatus.RESOLVED
    }

    created = reused = resolved = routed = 0
    for key, entries in by_item.items():
        first = entries[0]
        plan = matcher.plan_for(first)
        linked = (
            plan is not None and plan.source is PlanSource.CAPTURED and first.material_scope is MaterialScope.OAR
        )
        if not linked and key not in held:
            continue
        finding = (
            detect_not_issued(
                entries, as_of=as_of_time.date(), threshold_days=threshold_days, final_issued=key in final_issued
            )
            if linked
            else None
        )
        evidence = {"reservation_number": key[0], "reservation_item": key[1]}
        reason = "n/a"
        if finding is not None:
            evidence.update(basis=finding.basis.value, since=str(finding.since), days=str(finding.days))
            reason = (
                f"Received {finding.since} for this reservation, nothing issued after {finding.days} days"
                if finding.basis is NotIssuedBasis.RECEIVED
                else f"Required by {finding.since}, nothing issued after {finding.days} days"
            )
        c, r, res, rt = _apply_detection(
            exception_id=build_exception_id("NOT_ISSUED_30_DAYS", *key),
            condition_holds=finding is not None,
            exception_type=ExceptionType.NOT_ISSUED_30_DAYS,
            material=first.material,
            plant=first.plant,
            reservation_number=key[0],
            reservation_item=key[1],
            session_id=plan.session_id if plan is not None else None,
            ledger_entry_id=first.ledger_id,
            owner_requester_id=owner_for(plan, *key) if plan is not None else None,
            reason=reason,
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
    return created, reused, resolved, routed


def retire_no_plan_grni(*, as_of_time: datetime, repository: ExceptionRepository) -> int:
    """Resolve every NO_PLAN_GRNI exception still open (retired 5-Nov-2026).

    Detection no longer raises the type, so nothing else would ever close the
    ones already stored. Audited like any other resolution; idempotent, so
    every run may call it. Returns how many it resolved.
    """
    count = 0
    for exception in repository.list(exception_type=ExceptionType.NO_PLAN_GRNI):
        if exception.status is ExceptionStatus.RESOLVED:
            continue
        _transition(
            exception,
            ExceptionStatus.RESOLVED,
            repository=repository,
            as_of_time=as_of_time,
            actor_id=None,
            actor_type="SYSTEM",
            event_type=EventType.RESOLVED,
            metadata={"reason": "exception type retired 5-Nov-2026 (Non-AI + 30 days not issued)"},
            resolved_at=as_of_time,
        )
        count += 1
    return count


def detect_exceptions(
    as_of_time: datetime,
    *,
    ledger_entries: Sequence[ReservationLedgerEntry],
    plans: Sequence[ConsumptionPlan],
    repository: ExceptionRepository,
    notification_port: NotificationPort,
    plan_breach_grace_days: int,
    requester_response_days: int,
    quantity_decision_records: Sequence[QuantityDecisionRecord] = (),
    requester_by_reservation: Mapping[tuple[str, str], str] = {},
    no_plan_reason_by_reservation: Mapping[tuple[str, str], NoPlanReason] = {},
    requester_by_session: Mapping[str, str] = {},
    types: Collection[ExceptionType] | None = None,
    not_issued_days: int = 30,
    final_issued: Collection[tuple[str, str]] = frozenset(),
) -> DetectionRunResult:
    """Run PLAN_BREACH, NO_PLAN, NOT_ISSUED_30_DAYS and QUANTITY_OVERRIDE
    detection for the given already-fetched evidence, and persist/update/resolve
    exceptions accordingly. Fetching ``ledger_entries``/``plans``/
    ``requester_by_reservation`` (from W6.2/W6.4/CAPTURE) is the caller's job -- this function does no I/O of its own
    beyond the injected ports, so it is trivially unit-testable and callable
    from a route, a script, or a future scheduler without any change.

    ``requester_by_reservation`` maps ``(reservation_number,
    reservation_item)`` to the requester W6.4 resolved from ``RESB.WEMPF``.
    Defaulting it to empty keeps every existing caller and test working
    unchanged, and reproduces exactly the old behaviour: plan requester only.

    ``requester_by_session`` maps a session id to the person named in the assistant's Requester box,
    where an administrator has said who that name stands for (``I13_REQUESTER_RECIPIENTS``). A
    PLAN_BREACH whose plan came from such a session is owned by that person instead of the plan's
    recorded requester (the person who operated the assistant). Empty keeps the old behaviour.

    ``types`` limits the run to those exception types (``I13_DETECTION_TYPES``
    on the scheduled runs); ``None`` is every type. A type left out is not
    evaluated at all, so its existing exceptions are neither created nor
    resolved by this run -- they stay exactly as they were.

    ``not_issued_days`` is NOT_ISSUED_30_DAYS's threshold, and ``final_issued``
    the reservation items SAP has closed for further issues (RESB-KZEAR),
    which it never chases (:func:`detection.detect_not_issued`).
    """

    def wanted(exception_type: ExceptionType) -> bool:
        return types is None or exception_type in types

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

    # Reference plans match reservations by number, captured plans by the
    # reservations their ID is typed on; an unlinked captured plan matches none
    # (PlanMatcher), so it can breach and never clears another's NO_PLAN.
    matcher = PlanMatcher(list(plans), list(ledger_entries))

    for plan in plans if wanted(ExceptionType.PLAN_BREACH) else ():
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
            # requester recorded against it. The person named in the assistant's
            # Requester box wins when they are known (requester_by_session).
            owner_requester_id=(
                (requester_by_session.get(plan.session_id) if plan.session_id else None)
                or owner_for(plan, plan.reservation_number, plan.reservation_item)
            ),
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

    for entry in ledger_entries if wanted(ExceptionType.NO_PLAN) else ():
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
        if entry.reservation_deleted:
            # RESB.XLOEK: not a live demand. Clearing the reason makes NO_PLAN's
            # condition false, so an exception raised before the deletion is
            # resolved by this run rather than left open.
            no_plan_reason = None

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

    if wanted(ExceptionType.NOT_ISSUED_30_DAYS):
        c, r, res, rt = _detect_not_issued(
            ledger_entries,
            matcher,
            as_of_time=as_of_time,
            threshold_days=not_issued_days,
            final_issued=final_issued,
            # The person named in the assistant's Requester box first, as for a breach.
            owner_for=lambda plan, number, item: (
                (requester_by_session.get(plan.session_id) if plan.session_id else None)
                or owner_for(plan, number, item)
            ),
            response_period=response_period,
            repository=repository,
            notification_port=notification_port,
        )
        created += c
        reused += r
        resolved += res
        routed += rt

    for record in quantity_decision_records if wanted(ExceptionType.QUANTITY_OVERRIDE) else ():
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
            # Nothing to ask: the assistant does not let a requester keep more
            # than the suggestion without a reason, and that reason is the
            # confirmation. So no "Confirm Exception", no due date, no HOD.
            route=False,
        )
        created += c
        reused += r
        resolved += res
        routed += rt
        if evaluation.available and evaluation.override:
            _confirm_recorded_override(
                build_exception_id("QUANTITY_OVERRIDE", quantity_override_business_key(record)),
                record,
                as_of_time=as_of_time,
                repository=repository,
            )

    return DetectionRunResult(as_of_time=as_of_time, created=created, reused=reused, resolved=resolved, routed=routed)


def process_escalations(
    as_of_time: datetime,
    *,
    repository: ExceptionRepository,
    escalation_recipient_provider: EscalationRecipientProvider,
    notification_port: NotificationPort,
    limit: int | None = None,
    plants: Collection[str] | None = None,
) -> EscalationRunResult:
    """Escalate every ``AWAITING_REQUESTER`` exception whose
    ``requester_due_at`` has passed. The exception's status moves to
    ``ESCALATED`` either way -- that is the workflow milestone ("this is now
    overdue and needs HOD attention"); whether a real HOD identity was found
    is tracked separately in ``routing_status``/``current_assignee_id`` so a
    routing failure is visible without ever inventing a recipient (FRS
    §11/W6.6 instruction: never fabricate a routing target).

    ``limit`` and ``plants`` only narrow *which* overdue exceptions this run takes --
    the oldest overdue first, at most ``limit``, only at those plants. Each one is then
    escalated by exactly the same rules. Left out, every overdue exception is taken, as
    before (the manual ``POST /run/escalate`` does that)."""
    escalated = routing_pending = 0
    if limit is None and plants is None:
        due = [
            exc
            for exc in repository.list(status=ExceptionStatus.AWAITING_REQUESTER)
            if exc.requester_due_at is not None and as_of_time >= exc.requester_due_at
        ]
    else:
        wanted_plants = {p.upper() for p in plants} if plants is not None else None
        pick = getattr(repository, "list_due_for_escalation", None)
        if pick is not None:  # the SQL repository filters and limits in the database
            due = pick(as_of_time, plants=wanted_plants, limit=limit)
        else:
            due = sorted(
                (
                    exc
                    for exc in repository.list(status=ExceptionStatus.AWAITING_REQUESTER)
                    if exc.requester_due_at is not None
                    and as_of_time >= exc.requester_due_at
                    and (wanted_plants is None or exc.plant.upper() in wanted_plants)
                ),
                key=lambda exc: (exc.requester_due_at, exc.exception_id),
            )
            if limit is not None:
                due = due[:limit]
    # A quantity override is a record, not a question: never the HOD's to chase.
    due = [exc for exc in due if exc.exception_type is not ExceptionType.QUANTITY_OVERRIDE]
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
            subject = "Escalation Required – Exception Not Confirmed"
            body = _hod_message(updated, as_of_time)
            for channel in (NotificationChannel.PLATFORM_QUEUE, NotificationChannel.EMAIL):
                _notify(repository, notification_port, updated, channel, hod_id, subject=subject, body=body, as_of_time=as_of_time, event_type=EXCEPTION_ESCALATED)
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

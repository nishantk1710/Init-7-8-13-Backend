"""W6.6 ACT application-service tests: detection idempotency/dedup,
requester routing, requester confirmation, HOD escalation (including
routing failure), the audit trail, and notification-failure isolation.

Uses the in-memory ``FakeExceptionRepository``/``FakeNotificationPort``/
``FakeEscalationRecipientProvider`` from ``tests/i13/conftest.py`` -- no
database, no SQLAlchemy, matching how ``app.initiatives.i13.act`` is
designed to be tested (see that package's ``__init__.py``).
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.initiatives.i13.act.domain import (
    EventType,
    ExceptionStatus,
    ExceptionType,
    NotificationOutcome,
    RoutingStatus,
)
from app.initiatives.i13.act.service import detect_exceptions, process_escalations, submit_confirmation
from app.initiatives.i13.act.state_machine import InvalidTransitionError
from app.initiatives.i13.models import (
    GiLinkStatus,
    GrLinkStatus,
    LifecycleStatus,
    ReservationLedgerEntry,
    ReservationPrLinkStatus,
)
from app.initiatives.i13.plans import ConsumptionPlan
from app.shared.material_scope import MaterialScope
from tests.i13.conftest import FakeEscalationRecipientProvider, FakeExceptionRepository, FakeNotificationPort

UTC = timezone.utc


def _plan(**overrides) -> ConsumptionPlan:
    defaults = dict(
        plan_id="PLAN-1",
        session_id="SESS-1",
        reservation_number="1000000000",
        reservation_item="0001",
        material="MAT1",
        plant="1000",
        requester="REQ1",
        purpose="test",
        planned_quantity=Decimal("10"),
        planned_use_date=date(2026, 9, 10),
        status="OPEN",
    )
    defaults.update(overrides)
    return ConsumptionPlan(**defaults)


def _ledger_entry(*, issued_quantity: Decimal = Decimal("0"), **overrides) -> ReservationLedgerEntry:
    defaults = dict(
        ledger_id="LEDGER-1",
        reservation_number="1000000000",
        reservation_item="0001",
        material="MAT1",
        plant="1000",
        reservation_quantity=Decimal("10"),
        requirement_date=None,
        pr_number=None,
        pr_item=None,
        po_number=None,
        po_item=None,
        ordered_quantity=None,
        received_quantity=None,
        issued_quantity=issued_quantity,
        first_gr_date=None,
        last_gr_date=None,
        first_issue_date=None,
        last_issue_date=None,
        procurement_issued_quantity=None,
        direct_store_issued_quantity=None,
        lifecycle_status=LifecycleStatus.ORDERED,
        reservation_pr_link_status=ReservationPrLinkStatus.NO_PR_REFERENCE,
        gr_link_status=GrLinkStatus.NO_RECEIPTS,
        gi_link_status=GiLinkStatus.NOT_APPLICABLE,
        gi_link_reason=None,
        material_scope=MaterialScope.OAR,
    )
    defaults.update(overrides)
    return ReservationLedgerEntry(**defaults)


def _detect(
    as_of_time,
    *,
    plans=(),
    ledger_entries=(),
    repository=None,
    notification_port=None,
    grni_snapshots=None,
    requester_by_reservation=None,
    requester_by_session=None,
    types=None,
    quantity_decision_records=(),
):
    repository = repository or FakeExceptionRepository()
    notification_port = notification_port or FakeNotificationPort()
    result = detect_exceptions(
        as_of_time,
        ledger_entries=list(ledger_entries),
        plans=list(plans),
        grni_snapshots=grni_snapshots or {},
        repository=repository,
        notification_port=notification_port,
        plan_breach_grace_days=7,
        requester_response_days=5,
        requester_by_reservation=requester_by_reservation or {},
        requester_by_session=requester_by_session or {},
        types=types,
        quantity_decision_records=list(quantity_decision_records),
    )
    return repository, notification_port, result


# --- detection + routing ---


def test_plan_breach_detected_and_routed_to_requester() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository, notification_port, result = _detect(as_of_time, plans=[_plan()])

    assert result.created == 1
    assert result.routed == 1
    exceptions = repository.list(exception_type=ExceptionType.PLAN_BREACH)
    assert len(exceptions) == 1
    exception = exceptions[0]
    assert exception.status is ExceptionStatus.AWAITING_REQUESTER
    assert exception.owner_requester_id == "REQ1"
    assert exception.requester_due_at == as_of_time + timedelta(days=5)
    # Platform queue + email, both attempted.
    assert len(notification_port.sent) == 2


def test_a_plan_breach_is_owned_by_the_person_named_for_its_session() -> None:
    """The Requester box names someone the administrator has mapped: they own the breach, not the operator."""
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository, notification_port, result = _detect(
        as_of_time,
        plans=[_plan(requester="OPERATOR-GUID", session_id="SESS-1")],
        requester_by_session={"SESS-1": "darren"},
    )

    assert result.created == 1 and result.routed == 1
    [exception] = repository.list(exception_type=ExceptionType.PLAN_BREACH)
    assert exception.owner_requester_id == "darren"
    assert exception.current_assignee_id == "darren"
    # The notice is addressed to the named person, not to the operator.
    assert {sent.recipient for sent in notification_port.sent} == {"darren"}


def test_a_plan_breach_stays_with_the_operator_when_nobody_is_named_for_its_session() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    for named in ({}, {"OTHER-SESSION": "darren"}):
        repository, _, _ = _detect(
            as_of_time, plans=[_plan(requester="OPERATOR-GUID", session_id="SESS-1")], requester_by_session=named
        )
        [exception] = repository.list(exception_type=ExceptionType.PLAN_BREACH)
        assert exception.owner_requester_id == "OPERATOR-GUID"


def test_a_plan_with_no_session_is_never_matched_to_a_named_person() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository, _, _ = _detect(
        as_of_time, plans=[_plan(requester="OPERATOR-GUID", session_id="")], requester_by_session={"": "darren"}
    )
    [exception] = repository.list(exception_type=ExceptionType.PLAN_BREACH)
    assert exception.owner_requester_id == "OPERATOR-GUID"


def test_no_plan_exception_detected_for_oar_reservation_without_a_plan() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository, _, result = _detect(as_of_time, ledger_entries=[_ledger_entry()])
    assert result.created == 1
    no_plan = repository.list(exception_type=ExceptionType.NO_PLAN)
    assert len(no_plan) == 1
    assert no_plan[0].status is ExceptionStatus.OPEN  # no requester known -> stays unrouted


# --- owner resolution: plan requester, then W6.4 attribution ---
#
# A NO_PLAN exception has no plan by definition, so before the second source
# existed its owner was unconditionally None: nothing routed, nothing
# escalated, and FR-9's second half was dead for every one of them.


def test_no_plan_routes_to_the_requester_w6_4_resolved_from_resb() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository, notification_port, result = _detect(
        as_of_time,
        ledger_entries=[_ledger_entry()],
        requester_by_reservation={("1000000000", "0001"): "WEMPF-USER"},
    )

    assert result.routed == 1
    exception = repository.list(exception_type=ExceptionType.NO_PLAN)[0]
    assert exception.owner_requester_id == "WEMPF-USER"
    assert exception.status is ExceptionStatus.AWAITING_REQUESTER
    assert exception.requester_due_at == as_of_time + timedelta(days=5)
    assert len(notification_port.sent) == 2  # platform queue + email


def test_the_plan_requester_wins_over_the_attributed_one() -> None:
    """The person who actually spoke to the assistant outranks a field on a
    reservation row. Both exist here and they disagree."""
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository, _, _ = _detect(
        as_of_time,
        plans=[_plan()],
        requester_by_reservation={("1000000000", "0001"): "WEMPF-USER"},
    )

    exception = repository.list(exception_type=ExceptionType.PLAN_BREACH)[0]
    assert exception.owner_requester_id == "REQ1"


def test_an_unresolved_reservation_stays_unowned_rather_than_guessing() -> None:
    """W6.4 reports no requester for an AMBIGUOUS attribution, so nothing is
    in the map for it. That must leave the exception unrouted -- routing it to
    the wrong named person is worse, because somebody answers it."""
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository, notification_port, result = _detect(
        as_of_time,
        ledger_entries=[_ledger_entry()],
        requester_by_reservation={("9999999999", "0001"): "SOMEBODY-ELSE"},
    )

    assert result.routed == 0
    exception = repository.list(exception_type=ExceptionType.NO_PLAN)[0]
    assert exception.owner_requester_id is None
    assert exception.status is ExceptionStatus.OPEN
    assert notification_port.sent == []


def test_an_existing_unowned_exception_is_routed_once_a_requester_resolves() -> None:
    """Run 1 has no owner; run 2 does. The same row is reused and routed --
    detection is idempotent, but resolving an owner is not a no-op."""
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository = FakeExceptionRepository()
    _detect(as_of_time, ledger_entries=[_ledger_entry()], repository=repository)
    assert repository.list(exception_type=ExceptionType.NO_PLAN)[0].status is ExceptionStatus.OPEN

    _, _, result = _detect(
        as_of_time,
        ledger_entries=[_ledger_entry()],
        repository=repository,
        requester_by_reservation={("1000000000", "0001"): "WEMPF-USER"},
    )

    assert result.created == 0
    assert result.routed == 1
    exceptions = repository.list(exception_type=ExceptionType.NO_PLAN)
    assert len(exceptions) == 1  # still one row, not a second
    assert exceptions[0].owner_requester_id == "WEMPF-USER"
    assert exceptions[0].status is ExceptionStatus.AWAITING_REQUESTER


# --- idempotency / dedup ---


def test_detection_is_idempotent_across_repeated_runs() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository = FakeExceptionRepository()
    _detect(as_of_time, plans=[_plan()], repository=repository)
    _, _, result = _detect(as_of_time, plans=[_plan()], repository=repository)

    assert result.created == 0
    assert result.reused == 1
    assert len(repository.list(exception_type=ExceptionType.PLAN_BREACH)) == 1


def test_detection_auto_resolves_when_condition_clears() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository = FakeExceptionRepository()
    _detect(as_of_time, plans=[_plan()], repository=repository)

    entries = [_ledger_entry(issued_quantity=Decimal("1"))]
    _, _, result = _detect(as_of_time, plans=[_plan()], ledger_entries=entries, repository=repository)

    assert result.resolved == 1
    exception = repository.get("ACT-PLAN_BREACH-PLAN-1")
    assert exception.status is ExceptionStatus.RESOLVED
    assert exception.resolved_at == as_of_time


def test_resolved_exception_is_not_reopened_by_a_later_run() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository = FakeExceptionRepository()
    _detect(as_of_time, plans=[_plan()], repository=repository)
    entries = [_ledger_entry(issued_quantity=Decimal("1"))]
    _detect(as_of_time, plans=[_plan()], ledger_entries=entries, repository=repository)
    assert repository.get("ACT-PLAN_BREACH-PLAN-1").status is ExceptionStatus.RESOLVED

    # Condition "reappears" (no matching issue again) -- must not reopen.
    _, _, result = _detect(as_of_time, plans=[_plan()], repository=repository)
    assert result.created == 0
    assert repository.get("ACT-PLAN_BREACH-PLAN-1").status is ExceptionStatus.RESOLVED


# --- requester confirmation ---


def test_submit_confirmation_from_awaiting_requester_persists_and_transitions() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository, _, _ = _detect(as_of_time, plans=[_plan()])
    exception_id = "ACT-PLAN_BREACH-PLAN-1"

    updated = submit_confirmation(
        exception_id=exception_id,
        reason_category="STOCK_DELAYED",
        free_text="Awaiting vendor delivery",
        actor_id="REQ1",
        as_of_time=as_of_time + timedelta(hours=1),
        repository=repository,
    )

    assert updated.status is ExceptionStatus.CONFIRMED
    confirmation = repository.get_confirmation(exception_id)
    assert confirmation is not None
    assert confirmation.reason_category == "STOCK_DELAYED"
    assert confirmation.actor_id == "REQ1"

    events = repository.list_events(exception_id)
    event_types = [event.event_type for event in events]
    assert EventType.REQUESTER_CONFIRMED in event_types
    assert EventType.JUSTIFICATION_ADDED in event_types


def test_submit_confirmation_from_open_is_an_invalid_transition() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository, _, _ = _detect(as_of_time, ledger_entries=[_ledger_entry()])  # NO_PLAN -> OPEN, never routed
    exception_id = "ACT-NO_PLAN-1000000000-0001"
    assert repository.get(exception_id).status is ExceptionStatus.OPEN

    try:
        submit_confirmation(
            exception_id=exception_id,
            reason_category="X",
            free_text="Y",
            actor_id="REQ1",
            as_of_time=as_of_time,
            repository=repository,
        )
        assert False, "expected InvalidTransitionError"
    except InvalidTransitionError:
        pass

    # Rejected transition must not have persisted a confirmation or changed status.
    assert repository.get_confirmation(exception_id) is None
    assert repository.get(exception_id).status is ExceptionStatus.OPEN


def test_submit_confirmation_for_unknown_exception_raises_lookup_error() -> None:
    repository = FakeExceptionRepository()
    try:
        submit_confirmation(
            exception_id="ACT-DOES-NOT-EXIST",
            reason_category="X",
            free_text="Y",
            actor_id="REQ1",
            as_of_time=datetime(2026, 9, 18, tzinfo=UTC),
            repository=repository,
        )
        assert False, "expected LookupError"
    except LookupError:
        pass


# --- escalation ---


def test_escalation_after_due_date_with_resolved_hod() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository, notification_port, _ = _detect(as_of_time, plans=[_plan()])
    exception_id = "ACT-PLAN_BREACH-PLAN-1"
    assert repository.get(exception_id).requester_due_at == as_of_time + timedelta(days=5)

    escalation_provider = FakeEscalationRecipientProvider({"1000": "HOD1"})
    later = as_of_time + timedelta(days=5, minutes=1)
    result = process_escalations(
        later, repository=repository, escalation_recipient_provider=escalation_provider, notification_port=notification_port
    )

    assert result.escalated == 1
    assert result.routing_pending == 0
    exception = repository.get(exception_id)
    assert exception.status is ExceptionStatus.ESCALATED
    assert exception.routing_status is RoutingStatus.RESOLVED
    assert exception.current_assignee_id == "HOD1"


def test_no_escalation_before_due_date() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository, notification_port, _ = _detect(as_of_time, plans=[_plan()])
    escalation_provider = FakeEscalationRecipientProvider({"1000": "HOD1"})

    result = process_escalations(
        as_of_time + timedelta(days=1),
        repository=repository,
        escalation_recipient_provider=escalation_provider,
        notification_port=notification_port,
    )
    assert result.escalated == 0
    assert repository.get("ACT-PLAN_BREACH-PLAN-1").status is ExceptionStatus.AWAITING_REQUESTER


def test_no_escalation_for_already_resolved_exception() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository, notification_port, _ = _detect(as_of_time, plans=[_plan()])
    exception_id = "ACT-PLAN_BREACH-PLAN-1"
    submit_confirmation(
        exception_id=exception_id, reason_category="X", free_text="Y", actor_id="REQ1", as_of_time=as_of_time, repository=repository,
    )

    escalation_provider = FakeEscalationRecipientProvider({"1000": "HOD1"})
    result = process_escalations(
        as_of_time + timedelta(days=30), repository=repository, escalation_recipient_provider=escalation_provider, notification_port=notification_port
    )
    assert result.escalated == 0
    assert repository.get(exception_id).status is ExceptionStatus.CONFIRMED


def test_escalation_routing_failure_does_not_fabricate_a_recipient() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository, notification_port, _ = _detect(as_of_time, plans=[_plan()])
    exception_id = "ACT-PLAN_BREACH-PLAN-1"

    escalation_provider = FakeEscalationRecipientProvider({})  # no HOD mapped for plant 1000
    later = as_of_time + timedelta(days=5, minutes=1)
    result = process_escalations(
        later, repository=repository, escalation_recipient_provider=escalation_provider, notification_port=notification_port
    )

    assert result.escalated == 0
    assert result.routing_pending == 1
    exception = repository.get(exception_id)
    assert exception.status is ExceptionStatus.ESCALATED
    assert exception.routing_status is RoutingStatus.PENDING
    assert exception.current_assignee_id is None

    events = repository.list_events(exception_id)
    assert any(event.event_type is EventType.ROUTING_FAILED for event in events)


# --- notifications / audit ---


def test_notification_provider_failure_does_not_corrupt_exception_state() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository = FakeExceptionRepository()
    notification_port = FakeNotificationPort(raises=True)

    _, _, result = _detect(as_of_time, plans=[_plan()], repository=repository, notification_port=notification_port)

    assert result.created == 1
    exception = repository.get("ACT-PLAN_BREACH-PLAN-1")
    # The state transition (OPEN -> AWAITING_REQUESTER) still happened even
    # though every notification attempt raised.
    assert exception.status is ExceptionStatus.AWAITING_REQUESTER

    events = repository.list_events(exception.exception_id)
    failed_events = [event for event in events if event.event_type is EventType.NOTIFICATION_FAILED]
    assert len(failed_events) == 2  # platform queue + email, both recorded as failed


def test_every_transition_appends_an_audit_event() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository, _, _ = _detect(as_of_time, plans=[_plan()])
    exception_id = "ACT-PLAN_BREACH-PLAN-1"

    submit_confirmation(
        exception_id=exception_id, reason_category="X", free_text="Y", actor_id="REQ1", as_of_time=as_of_time, repository=repository,
    )

    events = repository.list_events(exception_id)
    event_types = [event.event_type for event in events]
    assert EventType.DETECTED in event_types
    assert EventType.ROUTED_TO_REQUESTER in event_types
    assert EventType.REQUESTER_CONFIRMED in event_types
    assert EventType.JUSTIFICATION_ADDED in event_types
    # Every event carries a from/to status pair usable for a full history view.
    assert all(event.timestamp is not None for event in events)


def test_a_deleted_reservation_raises_no_plan_exception() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository, _, result = _detect(as_of_time, ledger_entries=[_ledger_entry(reservation_deleted=True)])
    assert result.created == 0
    assert repository.list(exception_type=ExceptionType.NO_PLAN) == []


def test_deleting_a_reservation_resolves_its_open_no_plan_exception() -> None:
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    repository = FakeExceptionRepository()
    _detect(as_of_time, ledger_entries=[_ledger_entry()], repository=repository)

    _, _, result = _detect(as_of_time, ledger_entries=[_ledger_entry(reservation_deleted=True)], repository=repository)

    assert result.resolved == 1
    assert repository.get("ACT-NO_PLAN-1000000000-0001").status is ExceptionStatus.RESOLVED


# --- 4-Nov-2026: a run limited to some exception types (I13_DETECTION_TYPES) ---


def test_a_type_left_out_is_not_raised():
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    # The plan breaches; the second reservation has no plan (Non-AI).
    entries = [_ledger_entry(), _ledger_entry(ledger_id="LEDGER-2", reservation_item="0002")]

    repository, _, result = _detect(
        as_of_time, plans=[_plan()], ledger_entries=entries, types={ExceptionType.PLAN_BREACH}
    )

    assert len(repository.list(exception_type=ExceptionType.PLAN_BREACH)) == 1
    assert repository.list(exception_type=ExceptionType.NO_PLAN) == []
    assert result.created == 1


def test_a_type_left_out_is_not_resolved_either():
    """Leaving Non-AI out holds its queue as it is -- it does not clear it."""
    repository, _, _ = _detect(datetime(2026, 9, 18, tzinfo=UTC), ledger_entries=[_ledger_entry()])
    assert len(repository.list(exception_type=ExceptionType.NO_PLAN)) == 1

    # Later the reservation is deleted in SAP, which would resolve it -- but this run skips NO_PLAN.
    _detect(
        datetime(2026, 9, 25, tzinfo=UTC),
        ledger_entries=[_ledger_entry(reservation_deleted=True)],
        repository=repository,
        types={ExceptionType.PLAN_BREACH},
    )

    (still,) = repository.list(exception_type=ExceptionType.NO_PLAN)
    assert still.status is not ExceptionStatus.RESOLVED

    # Checked with NO_PLAN, the same evidence does resolve it.
    _detect(datetime(2026, 9, 26, tzinfo=UTC), ledger_entries=[_ledger_entry(reservation_deleted=True)], repository=repository)
    (resolved,) = repository.list(exception_type=ExceptionType.NO_PLAN)
    assert resolved.status is ExceptionStatus.RESOLVED


def test_no_types_given_is_every_type():
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    entries = [_ledger_entry(), _ledger_entry(ledger_id="LEDGER-2", reservation_item="0002")]
    repository, _, _ = _detect(as_of_time, plans=[_plan()], ledger_entries=entries)
    assert repository.list(exception_type=ExceptionType.PLAN_BREACH)
    assert repository.list(exception_type=ExceptionType.NO_PLAN)


# --- 4-Nov-2026: a quantity override is a record, not a question ---------------
#
# The assistant does not let a requester keep more than the suggestion without a
# reason, and that reason is the confirmation. So the exception is shown (it can
# be counted) but nobody is asked, notified, given a due date or escalated to.


def _override(**overrides):
    from app.initiatives.i13.act.domain import QuantityDecisionRecord

    fields = dict(
        material="MAT1",
        plant="1000",
        reservation_number=None,
        reservation_item=None,
        session_id="SLINE1",
        requester_id="REQ1",
        requested_quantity=Decimal("12"),
        suggested_quantity=Decimal("8"),
        suggestion_reason="ASSISTANT_FR3",
        override_justification="SHUTDOWN: two pumps out at once",
        override_reason_category="SHUTDOWN",
        override_free_text="two pumps out at once",
    )
    fields.update(overrides)
    return QuantityDecisionRecord(**fields)


QO_ID = "ACT-QUANTITY_OVERRIDE-SESSION-SLINE1"


def test_an_override_with_its_reason_is_confirmed_and_nobody_is_notified():
    as_of_time = datetime(2026, 11, 4, tzinfo=UTC)
    repository, notification_port, result = _detect(as_of_time, quantity_decision_records=[_override()])

    exception = repository.get(QO_ID)
    assert exception.status is ExceptionStatus.CONFIRMED
    assert exception.owner_requester_id == "REQ1"
    assert exception.requester_due_at is None
    assert notification_port.sent == []
    assert result.created == 1 and result.routed == 0
    confirmation = repository.get_confirmation(QO_ID)
    assert (confirmation.reason_category, confirmation.free_text) == ("SHUTDOWN", "two pumps out at once")

    # The next run keeps it as it is: no second confirmation, still nobody told.
    _detect(as_of_time + timedelta(days=1), quantity_decision_records=[_override()], repository=repository, notification_port=notification_port)
    assert repository.get(QO_ID).status is ExceptionStatus.CONFIRMED
    assert notification_port.sent == []


def test_an_override_without_a_reason_stays_open_and_is_not_sent_out():
    repository, notification_port, _ = _detect(
        datetime(2026, 11, 4, tzinfo=UTC),
        quantity_decision_records=[_override(override_justification=None, override_reason_category=None, override_free_text=None)],
    )

    exception = repository.get(QO_ID)
    assert exception.status is ExceptionStatus.OPEN
    assert exception.requester_due_at is None
    assert notification_port.sent == []


def test_one_still_waiting_from_before_is_confirmed_on_the_next_run():
    import dataclasses

    as_of_time = datetime(2026, 11, 4, tzinfo=UTC)
    no_reason = _override(override_justification=None, override_reason_category=None, override_free_text=None)
    repository, notification_port, _ = _detect(as_of_time, quantity_decision_records=[no_reason])
    # As overrides were raised before this change: sent to the requester, due in 5 days.
    repository.upsert(
        dataclasses.replace(
            repository.get(QO_ID),
            status=ExceptionStatus.AWAITING_REQUESTER,
            owner_requester_id="REQ1",
            requester_due_at=as_of_time + timedelta(days=5),
        )
    )

    _detect(as_of_time + timedelta(hours=1), quantity_decision_records=[_override()], repository=repository, notification_port=notification_port)

    assert repository.get(QO_ID).status is ExceptionStatus.CONFIRMED


def test_an_override_is_never_escalated_to_the_hod():
    import dataclasses

    as_of_time = datetime(2026, 11, 4, tzinfo=UTC)
    no_reason = _override(override_justification=None, override_reason_category=None, override_free_text=None)
    repository, notification_port, _ = _detect(as_of_time, quantity_decision_records=[no_reason])
    repository.upsert(
        dataclasses.replace(
            repository.get(QO_ID),
            status=ExceptionStatus.AWAITING_REQUESTER,
            owner_requester_id="REQ1",
            requester_due_at=as_of_time + timedelta(days=5),
        )
    )

    result = process_escalations(
        as_of_time + timedelta(days=6),
        repository=repository,
        escalation_recipient_provider=FakeEscalationRecipientProvider({"1000": "HOD1"}),
        notification_port=notification_port,
    )

    assert result.escalated == 0
    assert repository.get(QO_ID).status is ExceptionStatus.AWAITING_REQUESTER
    assert notification_port.sent == []


def test_a_plan_breach_is_still_sent_to_the_requester():
    """Only the override changed: every other type is routed as before."""
    repository, notification_port, _ = _detect(
        datetime(2026, 9, 18, tzinfo=UTC), plans=[_plan()], quantity_decision_records=[_override()]
    )
    assert repository.get("ACT-PLAN_BREACH-PLAN-1").status is ExceptionStatus.AWAITING_REQUESTER
    assert len(notification_port.sent) == 2  # the breach's platform + email; none for the override

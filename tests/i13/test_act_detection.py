"""W6.6 pure detection rule tests -- no database, no fake repositories,
matching every scenario in the W6.6 task spec (plan breach, no-plan,
no-plan/GRNI, quantity override)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.initiatives.i13.act.detection import (
    build_exception_id,
    classify_no_plan_reason,
    detect_plan_breach,
    detect_quantity_override,
    no_plan_grni_applies,
    quantity_override_business_key,
)
from app.initiatives.i13.act.domain import NoPlanReason, QuantityDecisionRecord
from app.initiatives.i13.models import (
    GiLinkStatus,
    GrLinkStatus,
    LifecycleStatus,
    ReservationLedgerEntry,
    ReservationPrLinkStatus,
)
from app.initiatives.i13.plans import ConsumptionPlan
from app.shared.material_scope import MaterialScope

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


# --- PLAN_BREACH ---


def test_plan_breach_raised_after_grace_period_with_no_matching_issue() -> None:
    plan = _plan(planned_use_date=date(2026, 9, 10))
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    assert detect_plan_breach(plan, [], as_of_time=as_of_time, grace_period=timedelta(days=7)) is True


def test_plan_breach_not_raised_when_matching_issue_exists() -> None:
    plan = _plan(planned_use_date=date(2026, 9, 10))
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    entries = [_ledger_entry(issued_quantity=Decimal("1"))]
    assert detect_plan_breach(plan, entries, as_of_time=as_of_time, grace_period=timedelta(days=7)) is False


def test_plan_breach_not_raised_before_grace_period_expires() -> None:
    plan = _plan(planned_use_date=date(2026, 9, 10))
    as_of_time = datetime(2026, 9, 15, tzinfo=UTC)
    assert detect_plan_breach(plan, [], as_of_time=as_of_time, grace_period=timedelta(days=7)) is False


def test_plan_breach_not_raised_for_non_open_plan() -> None:
    plan = _plan(planned_use_date=date(2026, 9, 10), status="CLOSED")
    as_of_time = datetime(2026, 9, 18, tzinfo=UTC)
    assert detect_plan_breach(plan, [], as_of_time=as_of_time, grace_period=timedelta(days=7)) is False


# --- NO_PLAN ---


def test_no_plan_missing_session_when_no_plan_record_exists() -> None:
    assert classify_no_plan_reason(None) is NoPlanReason.MISSING_SESSION


def test_no_plan_invalid_session_when_plan_has_blank_session_id() -> None:
    plan = _plan(session_id="   ")
    assert classify_no_plan_reason(plan) is NoPlanReason.INVALID_SESSION


def test_no_plan_session_without_plan_when_plan_is_not_open() -> None:
    plan = _plan(session_id="SESS-1", status="CANCELLED")
    assert classify_no_plan_reason(plan) is NoPlanReason.SESSION_WITHOUT_PLAN


def test_no_plan_not_raised_for_valid_session_and_plan() -> None:
    plan = _plan(session_id="SESS-1", status="OPEN", planned_quantity=Decimal("10"))
    assert classify_no_plan_reason(plan) is None


# --- NO_PLAN_GRNI (reuses W6.3's GRNI flag, never recalculated) ---


def test_no_plan_grni_applies_when_no_plan_and_watch_grni_flag_true() -> None:
    assert no_plan_grni_applies(NoPlanReason.MISSING_SESSION, True) is True


def test_no_plan_grni_does_not_apply_when_watch_grni_flag_false() -> None:
    assert no_plan_grni_applies(NoPlanReason.MISSING_SESSION, False) is False


def test_no_plan_grni_does_not_apply_when_plan_is_valid() -> None:
    assert no_plan_grni_applies(None, True) is False


def test_no_plan_grni_does_not_apply_when_grni_evidence_unavailable() -> None:
    assert no_plan_grni_applies(NoPlanReason.MISSING_SESSION, None) is False


# --- QUANTITY_OVERRIDE ---


def _quantity_record(**overrides) -> QuantityDecisionRecord:
    defaults = dict(
        material="MAT1",
        plant="1000",
        reservation_number="1000000000",
        reservation_item="0001",
        session_id="SESS-1",
        requester_id="REQ1",
        requested_quantity=Decimal("5"),
        suggested_quantity=Decimal("5"),
    )
    defaults.update(overrides)
    return QuantityDecisionRecord(**defaults)


def test_quantity_override_detected_with_variance() -> None:
    record = _quantity_record(requested_quantity=Decimal("12"), suggested_quantity=Decimal("5"))
    evaluation = detect_quantity_override(record)
    assert evaluation.available is True
    assert evaluation.override is True
    assert evaluation.variance == Decimal("7")


def test_quantity_override_not_detected_when_quantities_match() -> None:
    record = _quantity_record(requested_quantity=Decimal("5"), suggested_quantity=Decimal("5"))
    evaluation = detect_quantity_override(record)
    assert evaluation.available is True
    assert evaluation.override is False
    assert evaluation.variance == Decimal("0")


def test_quantity_override_unavailable_when_no_suggestion_source() -> None:
    record = _quantity_record(requested_quantity=Decimal("12"), suggested_quantity=None)
    evaluation = detect_quantity_override(record)
    assert evaluation.available is False
    assert evaluation.override is False
    assert evaluation.variance is None


# --- business keys ---


def test_build_exception_id_is_deterministic() -> None:
    assert build_exception_id("PLAN_BREACH", "PLAN-1") == build_exception_id("PLAN_BREACH", "PLAN-1")
    assert build_exception_id("PLAN_BREACH", "PLAN-1") != build_exception_id("PLAN_BREACH", "PLAN-2")


def test_quantity_override_business_key_prefers_reservation_reference() -> None:
    record = _quantity_record(reservation_number="1000000000", reservation_item="0001", session_id="SESS-1")
    assert quantity_override_business_key(record) == "1000000000-0001"


def test_quantity_override_business_key_falls_back_to_session_id() -> None:
    record = _quantity_record(reservation_number=None, reservation_item=None, session_id="SESS-9")
    assert quantity_override_business_key(record) == "SESSION-SESS-9"

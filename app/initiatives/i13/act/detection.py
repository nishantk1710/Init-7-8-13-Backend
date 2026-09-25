"""W6.6 pure detection rules -- no I/O, no ``datetime.now()``: every
function takes ``as_of_time``/its inputs as arguments so tests are
deterministic (see ``tests/i13/test_act_detection.py``).

Nothing here recomputes a W6.3 WATCH formula (months of cover, aging, GRNI,
acquired-vs-plan) or a W6.2 ledger-stitching rule -- both are consumed as
already-computed evidence (``WatchGrniSnapshot``, ``ReservationLedgerEntry``,
``ConsumptionPlan``).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from app.initiatives.i13.act.domain import (
    NoPlanReason,
    QuantityDecisionRecord,
    QuantityOverrideEvaluation,
)
from app.initiatives.i13.models import ReservationLedgerEntry
from app.initiatives.i13.plans import ConsumptionPlan


def build_exception_id(exception_type: str, *key_parts: str) -> str:
    """Deterministic business key -> the same unresolved condition always
    maps to the same exception id. See ``service.py``'s module docstring for
    the dedup rule per exception type this composes."""
    return "ACT-" + "-".join((exception_type, *key_parts))


def has_matching_issue(ledger_entries: list[ReservationLedgerEntry]) -> bool:
    """FRS: "no matching issue in the utilisation ledger" -- an issue exists
    once any goods-issue evidence has posted against the reservation. Locked
    rule: any issued quantity counts, not a proportional/partial-consumption
    threshold (no such formula is an approved requirement in this codebase
    today -- see the W6.6 task's explicit instruction not to invent one)."""
    return any(entry.issued_quantity > 0 for entry in ledger_entries)


def detect_plan_breach(
    plan: ConsumptionPlan,
    ledger_entries: list[ReservationLedgerEntry],
    *,
    as_of_time: datetime,
    grace_period: timedelta,
) -> bool:
    """breach_due_at = planned_window_end + grace_period; plan_breach =
    as_of_time > breach_due_at AND no matching issue exists.

    ``plan.breach_reference_date`` is the planned consumption window end: a
    captured plan's ``window_end`` when it has one, else the single
    ``planned_use_date`` the reference CSV carries (see ``plans.py``). A plan with no ``planned_use_date`` or that is not
    ``OPEN`` cannot breach: there is no window to measure against, and a
    closed/cancelled plan is not an active commitment.
    """
    breach_from = plan.breach_reference_date
    if breach_from is None or plan.status != "OPEN":
        return False
    breach_due_at = datetime.combine(breach_from, datetime.min.time(), tzinfo=as_of_time.tzinfo) + grace_period
    if as_of_time <= breach_due_at:
        return False
    return not has_matching_issue(ledger_entries)


def classify_no_plan_reason(plan: ConsumptionPlan | None) -> NoPlanReason | None:
    """A reservation has no valid session identifier, or references a
    session for which no valid consumption plan exists.

    Today's CAPTURE contract only carries a session_id as a field *on* a
    ``ConsumptionPlan`` record (``plans.py`` -- there is no independent
    session store), so the three reasons map onto it as:

    * no plan row at all for this reservation -> ``MISSING_SESSION`` (no
      session reference exists for it in this dataset).
    * a plan row exists but its ``session_id`` is blank -> ``INVALID_SESSION``
      (a plan was recorded with no usable session identifier).
    * a plan row exists with a ``session_id`` but the plan itself is not a
      valid/active one (``status`` other than ``OPEN``, or a non-positive
      planned quantity) -> ``SESSION_WITHOUT_PLAN`` (a session reference
      exists but no valid plan currently backs it).

    Returns ``None`` when a valid session + valid plan both exist -- no
    no-plan exception.
    """
    if plan is None:
        return NoPlanReason.MISSING_SESSION
    if not plan.session_id or not plan.session_id.strip():
        return NoPlanReason.INVALID_SESSION
    if plan.status != "OPEN" or plan.planned_quantity <= 0:
        return NoPlanReason.SESSION_WITHOUT_PLAN
    return None


def detect_quantity_override(record: QuantityDecisionRecord) -> QuantityOverrideEvaluation:
    """quantity_override = requested_quantity != suggested_quantity.

    ``record.suggested_quantity is None`` means the suggestion source
    (W7.4) did not answer -- reported as unavailable, never coerced into
    "no override". Never an automatic rejection: this only makes the
    deviation (and any existing justification) visible and auditable.
    """
    if record.suggested_quantity is None:
        return QuantityOverrideEvaluation(available=False, override=False, variance=None)
    suggested = Decimal(str(record.suggested_quantity))
    requested = Decimal(str(record.requested_quantity))
    variance = requested - suggested
    return QuantityOverrideEvaluation(available=True, override=variance != 0, variance=variance)


def quantity_override_business_key(record: QuantityDecisionRecord) -> str:
    """Smallest correct identity for a quantity-override exception: the
    reservation reference when known, else the session id, else
    material+plant as a last resort (so two decisions for the same
    material/plant with no reservation/session reference don't silently
    collide into different exceptions -- they collide into the same one,
    which is the conservative choice when identity is otherwise unknown)."""
    if record.reservation_number and record.reservation_item:
        return f"{record.reservation_number}-{record.reservation_item}"
    if record.session_id:
        return f"SESSION-{record.session_id}"
    return f"{record.material}-{record.plant}"


def no_plan_grni_applies(no_plan_reason: NoPlanReason | None, grni_flag: bool | None) -> bool:
    """The 30-day GRNI fallback for no-plan cases: reuses W6.3's
    ``gr_not_issued_flag`` (see ``domain.WatchGrniSnapshot``) unchanged --
    never a second GRNI calculation. ``grni_flag is None`` (no WATCH mart row
    computed yet for this material/plant) is treated as "not applicable",
    never coerced to ``True`` or ``False``."""
    return no_plan_reason is not None and grni_flag is True

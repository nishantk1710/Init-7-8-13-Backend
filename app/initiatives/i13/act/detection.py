"""W6.6 pure detection rules -- no I/O, no ``datetime.now()``: every
function takes ``as_of_time``/its inputs as arguments so tests are
deterministic (see ``tests/i13/test_act_detection.py``).

Nothing here recomputes a W6.3 WATCH formula (months of cover, aging, GRNI,
acquired-vs-plan) or a W6.2 ledger-stitching rule -- both are consumed as
already-computed evidence (``ReservationLedgerEntry``, ``ConsumptionPlan``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum

from app.initiatives.i13.act.domain import (
    NoPlanReason,
    QuantityDecisionRecord,
    QuantityOverrideEvaluation,
)
from app.initiatives.i13.models import ReservationLedgerEntry
from app.initiatives.i13.plans import ConsumptionPlan, stands_for_demand


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
    as_of_time > breach_due_at AND the plan has a reservation AND nothing was
    issued on it.

    ``plan.breach_reference_date`` is the planned consumption window end: a
    captured plan's ``window_end`` when it has one, else the single
    ``planned_use_date`` the reference CSV carries (see ``plans.py``). A plan with no ``planned_use_date`` or that is not
    ``OPEN`` cannot breach: there is no window to measure against, and a
    closed/cancelled plan is not an active commitment.

    "Nothing issued" is read off a reservation: the goods issues SAP posted
    against it (MSEG 261/201 less 262/202 by RSNUM/RSPOS). A plan no live
    reservation stands for (no item text carries its ID, or only a deleted
    item with nothing issued -- :func:`plans.stands_for_demand`) has nothing
    to check, so it is not overdue: it is "Awaiting reservation" (24-Nov-2026).
    Before, an empty list read as "no issue found" and every such plan went
    overdue.
    """
    breach_from = plan.breach_reference_date
    if breach_from is None or plan.status != "OPEN":
        return False
    breach_due_at = datetime.combine(breach_from, datetime.min.time(), tzinfo=as_of_time.tzinfo) + grace_period
    if as_of_time <= breach_due_at:
        return False
    live = [entry for entry in ledger_entries if stands_for_demand(entry)]
    if not live:
        return False
    return not has_matching_issue(live)


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
    """quantity_override = requested_quantity > suggested_quantity.

    Only keeping MORE than the suggestion is an override: the finding is stock
    reserved beyond what the history supports. Keeping less ties up less OAR
    stock, so it is the requester's call, not an exception -- the same rule as
    ``QuantityDecision.is_override`` (``app.initiatives.i13.quantity``).
    ``variance`` is still the signed difference, so a lower quantity reads as a
    negative variance rather than none.

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
    return QuantityOverrideEvaluation(available=True, override=variance > 0, variance=variance)


def quantity_override_business_key(record: QuantityDecisionRecord) -> str:
    """Smallest correct identity for a quantity-override exception: the
    reservation reference when known, else the session id, else
    material+plant as a last resort (so two decisions for the same
    material/plant with no reservation/session reference don't silently
    collide into different exceptions -- they collide into the same one,
    which is the conservative choice when identity is otherwise unknown).
    A key the record's source fixed (``exception_key``) comes first."""
    if record.exception_key:
        return record.exception_key
    if record.reservation_number and record.reservation_item:
        return f"{record.reservation_number}-{record.reservation_item}"
    if record.session_id:
        return f"SESSION-{record.session_id}"
    return f"{record.material}-{record.plant}"


class NotIssuedBasis(str, Enum):
    #: The reservation raised its own PR, and the PO on it was received.
    RECEIVED = "RECEIVED"
    #: No receipt of its own: OAR stock was already on the shelf.
    REQUIRED = "REQUIRED"
    #: As REQUIRED, measured from the planned use the requester gave in the
    #: assistant (25-Nov-2026): SAP's requirement date is only the day the
    #: reservation was made.
    PLANNED = "PLANNED"


@dataclass(frozen=True)
class NotIssuedFinding:
    basis: NotIssuedBasis
    #: The goods receipt (RECEIVED) or the requirement date (REQUIRED).
    since: date
    days: int


def detect_not_issued(
    entries: Sequence[ReservationLedgerEntry],
    *,
    as_of: date,
    threshold_days: int,
    final_issued: bool = False,
    planned_start: date | None = None,
    created_on: date | None = None,
) -> NotIssuedFinding | None:
    """NOT_ISSUED_30_DAYS for one reservation item: nothing issued against it
    ``threshold_days`` after its goods arrived, or after it was required.

    ``entries`` are the item's ledger entries (one per PO line its PR split
    over). Every date here is SAP's own and every issue is posted against this
    reservation (MSEG RSNUM/RSPOS) -- nothing is estimated:

    * **RECEIVED** -- the reservation raised a PR (RESB-BANFN) and a PO on it
      was first received (EKBE) at least ``threshold_days`` ago. Checked first:
      goods bought for this reservation are the stronger finding.
    * **REQUIRED** -- otherwise, its requirement date (RESB-BDTER) is at least
      ``threshold_days`` ago. OAR is normally issued from stock already on the
      shelf, so this is the usual case.
    * **PLANNED** -- the same, where the assistant's plan gives a window
      (``planned_start``): measured from the window start, but never before the
      reservation was made (``created_on``, RESB-CRDAT) -- nothing could be
      issued on it before it existed. SAP's requirement date is used instead
      only when somebody typed one (it differs from ``created_on``): SAP
      pre-fills it with the created date, which is all it held on every
      assistant reservation (checked on prod, 25-Nov-2026), and a plan running
      for a year must not be chased 30 days after it was reserved. The same
      order as the Ledger's Required by (26-Nov-2026).

    No finding when anything was issued, the item is deleted (RESB-XLOEK) or
    closed for further issues (RESB-KZEAR): there is nothing left to chase.
    """
    if not entries or final_issued:
        return None
    if any(entry.reservation_deleted for entry in entries):
        return None
    if any(entry.issued_quantity > 0 for entry in entries):
        return None
    received = [entry.first_gr_date for entry in entries if entry.po_number and entry.first_gr_date]
    if received:
        since = min(received)
        days = (as_of - since).days
        return NotIssuedFinding(NotIssuedBasis.RECEIVED, since, days) if days >= threshold_days else None
    required = entries[0].requirement_date
    # A requirement date somebody typed in SAP (one not just pre-filled from the
    # created date) comes first, as on the Ledger's Required by (26-Nov-2026).
    typed = required is not None and created_on is not None and required != created_on
    if planned_start is not None and not typed:
        since = max(planned_start, created_on) if created_on is not None else planned_start
        days = (as_of - since).days
        return NotIssuedFinding(NotIssuedBasis.PLANNED, since, days) if days >= threshold_days else None
    if required is None:
        return None
    days = (as_of - required).days
    return NotIssuedFinding(NotIssuedBasis.REQUIRED, required, days) if days >= threshold_days else None

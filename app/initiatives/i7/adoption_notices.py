"""Telling the Inventory Controller to change an approved recommendation in SAP, and reminding them.

Two entry points, one message shape:

* :func:`queue_handover_notice` -- called by ``notifications._queue`` the moment the last approver in the
  recommendation's route approves (Warehouse Supervisor for a stock item on the default route, Commercial Head for
  OAR; whoever the route ends with). Sends the first notice (day 0) to the Inventory Controller at the plant.
* :func:`run_adoption_notices` -- run on a timer by the notification scheduler. For every recommendation handed over
  to SAP whose monitoring window is still open it sends the reminder that is due, until SAP matches the approved
  values; when the outcome is decided it says so once.

Every message names the material, plant, final approver role, SAP's current values, the recommended values and the
deadline.

**The schedule follows the policy window**, never a number of its own. With the window ``W`` (``policy.adoption.
monitoring_window_days``, 7 by default), day 0 being the handover: the first notice at day 0, reminders from
``I7_ADOPTION_REMINDER_FIRST_DAY`` (3) every ``I7_ADOPTION_REMINDER_EVERY_DAYS`` (2) days, and the final reminder on
day ``W - 1``, one day before the window closes on day ``W``: 0, 3, 5, 6 for the default.

**What is and is not claimed.** The reminders say the update is *not yet confirmed*; they never say SAP was not
updated. "Not adopted" is only said when the adoption check itself says it -- which it does only once the window has
closed and SAP's change history covers all of it. An incomplete history stays Unknown: reminders go on until the
window closes, and no outcome notice is sent.

**No state table.** What was already sent is read from the notification rows' idempotency keys: each step's trigger id
names the handover and the step (``sap-20261105-d3``, ``sap-20261105-result``), so a second pass, a second worker or a restart adds nothing. A scheduler that
was down sends only the latest step that is due, not a burst of the ones it missed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.initiatives.i7.notifications import MODULE, review_path, role_holders
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations.types import AdoptionStatus, ApprovalRole
from app.integrations.notifications import templates
from app.integrations.notifications.models import NotificationEvent
from app.integrations.notifications.service import enqueue
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation

logger = logging.getLogger(__name__)

#: Who is told: the Inventory Controller at the recommendation's plant.
RECIPIENT_ROLE = ApprovalRole.INVENTORY_CONTROLLER
#: Outcomes are only announced for handovers this many windows old or newer, so switching this on does not tell
#: anybody about last quarter's recommendations.
_LOOKBACK_WINDOWS = 2


@dataclass(frozen=True)
class AdoptionNoticeResult:
    reminders: int = 0
    outcomes: int = 0


# --- the schedule -------------------------------------------------------------------


def reminder_days(window_days: int, first_day: int, every_days: int) -> list[int]:
    """The days (counted from the handover, which is day 0) a message goes out, for a window of ``window_days``.

    Day 0, then ``first_day``, ``first_day + every_days``, ... while that is before the final reminder, then the
    final reminder on ``window_days - 1``. A window too short for a step simply has fewer of them (1 day: day 0 only).
    """
    days = [0]
    final = window_days - 1
    day = max(first_day, 1)
    while day < final:
        days.append(day)
        day += max(every_days, 1)
    if final >= 1:
        days.append(final)
    return days


def trigger_id(handed_off_on: date, step: str) -> str:
    """The idempotency name of one message: the handover it belongs to and the step (``d3``, ``result``).

    The handover date is in it so a second handover of the same recommendation (a regenerated one approved again)
    is a new cycle with its own messages, instead of being silenced by the first cycle's.
    """
    return f"sap-{handed_off_on:%Y%m%d}-{step}"


def deadline_of(handed_off_on: date, window_days: int) -> date:
    """The last day SAP can still be changed -- the day the adoption page's window closes."""
    return handed_off_on + timedelta(days=window_days)


# --- the message --------------------------------------------------------------------


def _quantity(value: Decimal | None) -> str:
    if value is None:
        return "not set"
    return format(value.normalize(), "f")


def values_text(row: Recommendation) -> str:
    """What SAP holds now against what was approved, one line per value the recommendation sets."""
    lines: list[str] = []
    if row.is_oar and row.conversion_eligibility == "ELIGIBLE":
        lines.append("MRP type: change to VB (OAR conversion)")
    for label, current, recommended in (
        ("Safety stock", row.current_safety_stock, row.recommended_safety_stock),
        ("Reorder point", row.current_rop, row.recommended_rop),
        ("Maximum stock", row.current_max_stock, row.recommended_max_stock),
    ):
        if recommended is not None:
            lines.append(f"{label}: SAP now {_quantity(current)} → recommended {_quantity(recommended)}")
    return "\n".join(lines)


def _final_approver_role(db: Session, recommendation_id: str) -> str:
    """The role that gave the final approval: the actor of the approval that handed it to SAP."""
    entry = db.scalars(
        select(ApprovalLedgerEntry)
        .where(
            ApprovalLedgerEntry.recommendation_id == recommendation_id,
            ApprovalLedgerEntry.action == "APPROVE",
            ApprovalLedgerEntry.new_status == "SAP_EXECUTION_PENDING",
        )
        .order_by(ApprovalLedgerEntry.id.desc())
        .limit(1)
    ).first()
    return entry.actor_role if entry is not None else "-"


def _context(db: Session, row: Recommendation, deadline: date, today: date, approver_role: str | None = None) -> dict[str, str]:
    return {
        "material": row.sap_material_number,
        "approver_role": approver_role or _final_approver_role(db, row.recommendation_id),
        "values": values_text(row),
        "deadline": deadline.strftime("%d %b %Y"),
        "days_left": str(max((deadline - today).days, 0)),
    }


def _send(db: Session, row: Recommendation, event_type: str, trigger: str, context: dict[str, str], now: datetime | None) -> int:
    event = NotificationEvent(
        module=MODULE,
        event_type=event_type,
        entity_type="RECOMMENDATION",
        entity_id=row.recommendation_id,
        trigger_id=trigger,
        action_path=review_path(row.recommendation_id, decide=False),
        plant=row.sap_plant_code,
        context=context,
    )
    created = enqueue(db, event, role_holders(db, RECIPIENT_ROLE, row.sap_plant_code), now=now)
    return sum(1 for note in created if note.channel == "IN_APP")


def _step_type(day: int, days: list[int]) -> str:
    if day == 0:
        return templates.APPROVAL_SAP_UPDATE_REQUIRED
    if day == days[-1]:
        return templates.APPROVAL_SAP_UPDATE_FINAL_REMINDER
    return templates.APPROVAL_SAP_UPDATE_REMINDER


# --- at the final approval ----------------------------------------------------------


def queue_handover_notice(db: Session, row: Recommendation, entry: ApprovalLedgerEntry, policy: PolicyDocument) -> None:
    """The first notice, queued with the final approval. Does nothing when there is nothing to change in SAP."""
    settings = get_settings()
    window_days = policy.adoption.monitoring_window_days
    if not settings.i7_adoption_notices_enabled or window_days is None:
        return
    if not values_text(row):
        return
    stamp = entry.timestamp or datetime.now(timezone.utc)
    handed_off_on = stamp.date()
    context = _context(db, row, deadline_of(handed_off_on, window_days), handed_off_on, approver_role=entry.actor_role)
    _send(db, row, templates.APPROVAL_SAP_UPDATE_REQUIRED, trigger_id(handed_off_on, "d0"), context, None)


# --- the timer ----------------------------------------------------------------------

_OUTCOME_WORDS = {
    AdoptionStatus.ADOPTED: "Adopted",
    AdoptionStatus.PARTIALLY_ADOPTED: "Partially adopted",
    AdoptionStatus.NOT_ADOPTED: "Not adopted",
}


def run_adoption_notices(db: Session, now: datetime | None = None, settings=None) -> AdoptionNoticeResult:
    """One pass. Queues the notices; the caller commits."""
    settings = settings or get_settings()
    if not settings.i7_adoption_notices_enabled:
        return AdoptionNoticeResult()
    policy = PolicyDocument()
    window_days = policy.adoption.monitoring_window_days
    if window_days is None:
        return AdoptionNoticeResult()
    now = now or datetime.now(timezone.utc)
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    today = now.date()

    # Imported here: the adoption endpoints import a great deal, and the check below is exactly theirs.
    from app.api.i7.adoption import HANDED_OFF_STATUSES, _build_view, _handoff_dates
    from app.api.i7.recommendations import _apply_filters, _latest_only

    rows = db.execute(
        _latest_only(
            _apply_filters(
                select(Recommendation),
                status=None, plant=None, material=None, demand_class=None, is_oar=None, confidence=None,
                criticality=None, generated_from=None, generated_to=None, actionable=True,
            )
        ).where(Recommendation.status.in_(HANDED_OFF_STATUSES))
    ).scalars().all()
    handed_off = _handoff_dates(db, [row.recommendation_id for row in rows])
    days = reminder_days(window_days, settings.i7_adoption_reminder_first_day, settings.i7_adoption_reminder_every_days)

    reminders = outcomes = 0
    for row in rows:
        handed_off_on = handed_off.get(row.recommendation_id)
        elapsed = (today - handed_off_on).days if handed_off_on else None
        if elapsed is None or elapsed < 0 or elapsed > window_days * _LOOKBACK_WINDOWS:
            continue
        try:
            view = _build_view(db, row, policy, handed_off_on, today)
            if not view.result.expected:
                continue
            context = _context(db, row, deadline_of(handed_off_on, window_days), today)
            status = view.result.status
            if status is AdoptionStatus.ADOPTED or (not view.window_open and status in _OUTCOME_WORDS):
                # Decided: say so once, and no more reminders.
                outcome = _OUTCOME_WORDS[status]
                outcomes += _send(
                    db, row, templates.APPROVAL_SAP_UPDATE_RESULT, trigger_id(handed_off_on, "result"),
                    {**context, "outcome": outcome, "detail": view.detail[:400]}, now,
                )
            elif view.window_open:
                due = max((day for day in days if day <= elapsed), default=None)
                if due is not None:
                    reminders += _send(db, row, _step_type(due, days), trigger_id(handed_off_on, f"d{due}"), context, now)
        except Exception:  # noqa: BLE001 -- one recommendation must not stop the others
            logger.exception("I07 SAP adoption notices: could not check %s", row.recommendation_id)
    return AdoptionNoticeResult(reminders=reminders, outcomes=outcomes)


def run_job(settings=None) -> str:
    """The timer's entry point: one pass in its own session, committed."""
    settings = settings or get_settings()
    if not settings.i7_adoption_notices_enabled:
        return "disabled (I7_ADOPTION_NOTICES_ENABLED is false)"
    from app.core.db import get_sessionmaker

    with get_sessionmaker()() as db:
        result = run_adoption_notices(db, settings=settings)
        db.commit()
    return f"{result.reminders} SAP update reminder(s), {result.outcomes} outcome notice(s)"

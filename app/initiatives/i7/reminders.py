"""Reminders and escalation for approvals that are waiting too long.

Run on a timer by the notification scheduler. For each recommendation that is
waiting on an approver it asks: has this stage waited long enough for a reminder,
or long enough to escalate? The timings come from the ``notification_sla`` table;
where that has no active row for a step, from the ``I7_*`` day settings
(:func:`settings_sla`). **With neither, nothing is reminded or escalated**; the
notifications sent when an approval moves (``notifications.py``) do not depend
on it at all.

What keeps it safe to run repeatedly, on several workers, and across restarts:

* The state is in the database (``notification_reminder_state``), not in memory.
* Every message has an idempotency key naming the stage and the reminder number,
  so even a repeat of the same pass adds nothing.
* The latest status is read each pass. A recommendation that was approved,
  rejected, held or sent anywhere else is no longer waiting on that approver.
* The current stage is the latest ledger entry. When the recommendation moves on
  that entry changes, so the count starts again and the previous approver is not
  reminded.
* After a reminder the next one is due one repeat interval *from now*, so a
  scheduler that was down does not fire a burst of catch-up reminders.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.initiatives.i7.notifications import MODULE, APPROVAL_ROLE_TO_ENTRA, pending_role, review_path, role_holders
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations.types import LifecycleStatus
from app.integrations.notifications import templates
from app.integrations.notifications.config_store import active_sla_for
from app.integrations.notifications.models import NotificationEvent
from app.integrations.notifications.resolver import resolve_role
from app.integrations.notifications.service import enqueue
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation
from app.models.notification import NotificationReminderState, NotificationSla

logger = logging.getLogger(__name__)

#: Statuses where an approver is the one who has to act. HELD is left out on purpose:
#: a hold is a deliberate pause, so it is not "waiting too long".
WAITING = (LifecycleStatus.PENDING_APPROVAL.value, LifecycleStatus.SENT_BACK.value)


@dataclass(frozen=True)
class ReminderResult:
    reminders: int = 0
    escalations: int = 0


@dataclass(frozen=True)
class SlaTiming:
    """Reminder timings in minutes, shaped like a ``notification_sla`` row, for the ones that come from settings."""

    first_reminder_minutes: int
    repeat_minutes: int
    max_reminders: int
    escalate_after_minutes: int | None
    escalation_role: str | None


_MINUTES_PER_DAY = 24 * 60


def settings_sla(settings=None) -> SlaTiming | None:
    """The ``I7_*`` day settings as timings, or ``None`` when they are off.

    Off means ``I7_REMINDER_FIRST_DAYS`` and ``I7_ESCALATE_AFTER_DAYS`` are both 0 (the default). Reminders need a
    positive first interval and maximum; escalation needs a positive number of days **and** a role, so a role with
    no days, or days with no role, never half-escalates.
    """
    settings = settings or get_settings()
    first, escalate = settings.i7_reminder_first_days, settings.i7_escalate_after_days
    role = (settings.i7_escalation_role or "").strip() or None
    reminders_on = first > 0 and settings.i7_reminder_max > 0
    escalation_on = escalate > 0 and role is not None
    if not reminders_on and not escalation_on:
        return None
    return SlaTiming(
        first_reminder_minutes=max(first, 1) * _MINUTES_PER_DAY,
        repeat_minutes=max(settings.i7_reminder_repeat_days, 1) * _MINUTES_PER_DAY,
        max_reminders=settings.i7_reminder_max if reminders_on else 0,
        escalate_after_minutes=escalate * _MINUTES_PER_DAY if escalation_on else None,
        escalation_role=role if escalation_on else None,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _is_latest(db: Session, row: Recommendation) -> bool:
    """False when a newer regeneration of the same recommendation exists."""
    newest = db.scalars(
        select(Recommendation.id)
        .where(Recommendation.recommendation_id == row.recommendation_id)
        .order_by(Recommendation.generated_at.desc(), Recommendation.id.desc())
        .limit(1)
    ).first()
    return newest == row.id


def run_reminders(db: Session, now: datetime | None = None) -> ReminderResult:
    """One pass. Queues the messages and saves the state; the caller commits."""
    now = _aware(now or datetime.now(timezone.utc))
    from_settings = settings_sla()
    has_sla_row = db.scalars(select(NotificationSla.id).where(NotificationSla.active).limit(1)).first() is not None
    if not has_sla_row and from_settings is None:
        return ReminderResult()

    policy = PolicyDocument()
    reminders = escalations = 0
    for row in list(db.scalars(select(Recommendation).where(Recommendation.status.in_(WAITING)))):
        try:
            sent, escalated = _check_one(db, row, policy, now, from_settings)
        except Exception:  # noqa: BLE001 -- one bad recommendation must not stop the rest
            logger.exception("I07 reminders: could not check %s", row.recommendation_id)
            continue
        reminders += sent
        escalations += escalated
    return ReminderResult(reminders=reminders, escalations=escalations)


def _check_one(
    db: Session, row: Recommendation, policy: PolicyDocument, now: datetime, from_settings: SlaTiming | None = None
) -> tuple[int, int]:
    if not _is_latest(db, row):
        return 0, 0
    role = pending_role(row, policy)
    entry = db.scalars(
        select(ApprovalLedgerEntry)
        .where(ApprovalLedgerEntry.recommendation_id == row.recommendation_id)
        .order_by(ApprovalLedgerEntry.id.desc())
        .limit(1)
    ).first()
    if role is None or entry is None:
        return 0, 0

    entra_role = APPROVAL_ROLE_TO_ENTRA[role]
    # A row in notification_sla (most specific first) always wins; the I7_* settings are what is used when there is none.
    sla = active_sla_for(db, "OAR" if row.is_oar else "ROP_MAX", entra_role) or from_settings
    if sla is None:
        return 0, 0

    since = _aware(entry.timestamp)
    state = db.scalars(
        select(NotificationReminderState).where(
            NotificationReminderState.recommendation_id == row.recommendation_id,
            NotificationReminderState.pending_since_ledger_id == entry.id,
        )
    ).first()
    if state is None:
        state = NotificationReminderState(
            recommendation_id=row.recommendation_id, pending_since_ledger_id=entry.id, reminders_sent=0
        )
        db.add(state)

    def event(event_type: str, trigger: str, *, decide: bool) -> NotificationEvent:
        return NotificationEvent(
            module=MODULE,
            event_type=event_type,
            entity_type="RECOMMENDATION",
            entity_id=row.recommendation_id,
            trigger_id=trigger,
            action_path=review_path(row.recommendation_id, decide=decide),
            plant=row.sap_plant_code,
            context={"material": row.sap_material_number, "pending_role": role.value},
        )

    sent = escalated = 0

    if state.reminders_sent < sla.max_reminders:
        due_at = _aware(state.next_due_at) if state.next_due_at else since + timedelta(minutes=sla.first_reminder_minutes)
        if now >= due_at:
            number = state.reminders_sent + 1
            enqueue(
                db,
                event(templates.APPROVAL_REMINDER, f"{entry.id}:reminder:{number}", decide=True),
                role_holders(db, role, row.sap_plant_code),
                now=now,
            )
            state.reminders_sent = number
            state.last_reminder_at = now
            state.next_due_at = now + timedelta(minutes=sla.repeat_minutes)
            sent = 1

    if (
        sla.escalate_after_minutes is not None
        and sla.escalation_role
        and state.escalated_at is None
        and now >= since + timedelta(minutes=sla.escalate_after_minutes)
    ):
        enqueue(
            db,
            event(templates.APPROVAL_ESCALATED, f"{entry.id}:escalated", decide=False),
            resolve_role(db, sla.escalation_role, row.sap_plant_code, fallback_email=get_settings().notification_fallback_email),
            now=now,
        )
        state.escalated_at = now
        escalated = 1

    return sent, escalated


def run_job() -> str:
    """The timer's entry point: one pass in its own session, committed."""
    from app.core.db import get_sessionmaker

    with get_sessionmaker()() as db:
        result = run_reminders(db)
        db.commit()
    return f"{result.reminders} reminder(s), {result.escalations} escalation(s)"

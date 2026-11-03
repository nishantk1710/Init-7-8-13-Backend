"""The jobs the notification scheduler runs.

Each opens its own database session, commits when done and closes it. They live
here, not in the scheduler, so the shared notification package never imports an
initiative.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.integrations.notifications.scheduler import Job

logger = logging.getLogger(__name__)


def _deliver_pending() -> str:
    """Send queued emails and retry failed ones."""
    from app.core.db import get_sessionmaker
    from app.integrations.notifications.service import dispatch_with_settings

    with get_sessionmaker()() as db:
        result = dispatch_with_settings(db)
    return f"{result.sent} sent, {result.logged_only} logged only, {result.failed} failed"


def _approval_reminders() -> str:
    """Remind and escalate approvals that have waited past their configured time."""
    from app.initiatives.i7.reminders import run_job

    return run_job()


def run_requester_escalations(settings=None, *, session_factory=None, now=None) -> str:
    """Escalate exceptions the requester has not answered within the response period.

    Does nothing unless ``NOTIFICATION_I13_ESCALATION_ENABLED`` is true. Even then it only
    handles plants that have a HOD in ``I13_HOD_RECIPIENTS`` (so no exception is moved to
    "escalated" with nobody to escalate to), and at most ``NOTIFICATION_I13_ESCALATION_BATCH_SIZE``
    per run, oldest overdue first -- a backlog is worked through over several runs.
    """
    from app.core.config import get_settings
    from app.initiatives.i13.act_hod_provider import ConfigEscalationRecipientProvider
    from app.initiatives.i13.config import build_i13_config

    settings = settings or get_settings()
    if not settings.notification_i13_escalation_enabled:
        return "disabled (NOTIFICATION_I13_ESCALATION_ENABLED is false)"
    limit = settings.notification_i13_escalation_batch_size
    if limit < 1:
        logger.error("I13 escalation not run: NOTIFICATION_I13_ESCALATION_BATCH_SIZE must be at least 1, got %s", limit)
        return "skipped: invalid batch size"

    config = build_i13_config(settings)
    plants = ConfigEscalationRecipientProvider.from_config_string(config.escalation.hod_recipients_raw).plants()
    if not plants:
        logger.warning("I13 escalation not run: I13_HOD_RECIPIENTS names no HOD, so there is nobody to escalate to")
        return "skipped: no HOD configured"

    if session_factory is None:
        from app.core.db import get_sessionmaker

        session_factory = get_sessionmaker()
    from app.initiatives.i13.act_runner import run_escalations

    try:
        with session_factory() as db:
            result = run_escalations(db, config, as_of_time=now or datetime.now(timezone.utc), limit=limit, plants=plants)
            db.commit()
    except Exception:
        logger.exception("I13 escalation failed; nothing from this batch was saved")
        raise
    processed = result.escalated + result.routing_pending
    if processed >= limit:
        logger.info("I13 escalation: batch of %d full; more overdue exceptions may remain for the next run", limit)
    return f"{processed} processed ({result.escalated} escalated, {result.routing_pending} with no HOD found), batch limit {limit}"


def _requester_escalations() -> str:
    return run_requester_escalations()


def build_jobs() -> list[Job]:
    # Reminders and escalations run before delivery, so what they queue goes out in the same tick.
    return [
        Job("approval_reminders", _approval_reminders),
        Job("requester_escalations", _requester_escalations),
        Job("deliver_pending", _deliver_pending),
    ]

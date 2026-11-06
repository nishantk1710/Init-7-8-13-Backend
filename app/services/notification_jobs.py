"""The jobs the notification scheduler runs.

Each opens its own database session, commits when done and closes it. They live
here, not in the scheduler, so the shared notification package never imports an
initiative.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path

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


#: The UTC date of this process's last completed detection run. In memory on purpose:
#: detection only creates what is missing, keeps what exists and resolves what cleared,
#: so a second run on the same day (after a restart, or on a second instance) changes
#: nothing and notifies nobody twice -- it only costs the time it takes.
_last_detection_date: date | None = None


def run_exception_detection(settings=None, *, session_factory=None, now=None) -> str:
    """Run I13 exception detection over every material, once a day.

    Does nothing unless ``NOTIFICATION_I13_DETECTION_ENABLED`` is true, and nothing
    before ``NOTIFICATION_I13_DETECTION_HOUR_UTC`` or after a run today -- including
    one :func:`detect_after_build` made. Reads the I13 snapshot like
    ``POST /api/i13/act/run/detect`` does; while there is none yet (first build
    running, or failed) it skips and tries again on the next tick.
    """
    from app.core.config import get_settings

    settings = settings or get_settings()
    if not settings.notification_i13_detection_enabled:
        return "disabled (NOTIFICATION_I13_DETECTION_ENABLED is false)"
    now = now or datetime.now(timezone.utc)
    hour = settings.notification_i13_detection_hour_utc
    if not 0 <= hour <= 23:
        logger.error("I13 detection not run: NOTIFICATION_I13_DETECTION_HOUR_UTC must be 0-23, got %s", hour)
        return "skipped: invalid hour"
    if _last_detection_date == now.date():
        return "already ran today"
    if now.hour < hour:
        return f"waiting for {hour:02d}:00 UTC"
    return _detect_everything(settings, session_factory=session_factory, now=now)


def detect_after_build(reason: str, settings=None, *, session_factory=None, now=None) -> str:
    """Run I13 exception detection over every material, now: called when a
    snapshot build or re-age has just succeeded (``snapshot_store.lifecycle``),
    so the exception queue -- and the dashboard cards that count it -- move with
    the data the screens show. Does nothing unless ``I13_DETECT_AFTER_BUILD``."""
    from app.core.config import get_settings

    settings = settings or get_settings()
    if not settings.i13_detect_after_build:
        return "disabled (I13_DETECT_AFTER_BUILD is false)"
    logger.info("I13 detection after the snapshot build (%s)", reason)
    return _detect_everything(settings, session_factory=session_factory, now=now or datetime.now(timezone.utc))


def _detect_everything(settings, *, session_factory, now: datetime) -> str:
    """One detection run over every material, of the ``I13_DETECTION_TYPES``."""
    global _last_detection_date
    from app.initiatives.i13.config import build_i13_config

    if session_factory is None:
        from app.core.db import get_sessionmaker

        session_factory = get_sessionmaker()
    from app.initiatives.i13.act_runner import detection_types, run_detection
    from app.initiatives.i13.snapshot import (
        SnapshotBuilding,
        SnapshotFailed,
        get_i13_snapshot,
    )
    from app.initiatives.i13.snapshot_store import lifecycle as store

    types = detection_types(settings.i13_detection_types)
    try:
        with session_factory() as db:
            snapshot = None
            if settings.i13_snapshot_enabled:
                try:
                    snapshot = store.current(db) if store.enabled() else get_i13_snapshot()
                except (SnapshotBuilding, SnapshotFailed) as unavailable:
                    return f"skipped: I13 snapshot not available ({unavailable})"
            result = run_detection(
                db, build_i13_config(settings), Path(settings.i13_data_dir), as_of_time=now, snapshot=snapshot, types=types
            )
            db.commit()
    except Exception:
        logger.exception("I13 detection failed; nothing from this run was saved")
        raise
    _last_detection_date = now.date()
    scope = ", ".join(sorted(t.value for t in types)) if types else "all types"
    return f"{result.created} created, {result.reused} kept, {result.resolved} resolved, {result.routed} routed ({scope})"


def _exception_detection() -> str:
    return run_exception_detection()


def _requester_escalations() -> str:
    return run_requester_escalations()


def _request_link_notices() -> str:
    """Tell requesters their request was linked to an SAP reservation, or remind them it is not yet."""
    from app.assistant.link_notifications import run_job

    return run_job()


def _sap_adoption_notices() -> str:
    """Remind the Inventory Controller to change approved values in SAP, while the monitoring window is open."""
    from app.initiatives.i7.adoption_notices import run_job

    return run_job()


def build_jobs() -> list[Job]:
    # Reminders, detection and escalations run before delivery, so what they queue goes out in the same tick.
    return [
        Job("approval_reminders", _approval_reminders),
        Job("sap_adoption_notices", _sap_adoption_notices),
        # Before escalations, so an exception raised or resolved tonight is seen by them.
        Job("exception_detection", _exception_detection),
        Job("requester_escalations", _requester_escalations),
        Job("request_link_notices", _request_link_notices),
        Job("deliver_pending", _deliver_pending),
    ]

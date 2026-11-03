"""The background timer for notifications, started with the backend itself.

One daemon thread wakes every ``NOTIFICATION_SCHEDULER_INTERVAL_SECONDS`` and
runs each registered job in turn. It starts in ``app.main``'s lifespan and stops
with the shared ``stop`` event, like the other timers there.

Safe with several workers or instances: before a job runs it takes a SQL Server
application lock named for that job (the same ``sp_getapplock`` lock the delta
scheduler uses), with no waiting. A worker that does not get the lock skips the
job this time, because another worker is running it. The lock only stops two
passes overlapping. It is not what prevents duplicate messages: that is the
idempotency key on every notification and the reminder state in the database.

A job that raises is logged and the loop carries on with the next job and the
next tick.

The jobs are plain functions handed in by the caller (see
``app.services.notification_jobs``), so this module does not import I07 or I13.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

LOCK_PREFIX = "spares_ai_notif_"


@dataclass(frozen=True)
class Job:
    """A named unit of work. ``run`` returns a short description of what it did."""

    name: str
    run: Callable[[], str]


def _default_lock(name: str):
    # Imported here so importing this module never reaches the database.
    from app.ingest.scheduler import _Lock

    return _Lock(name)


def run_job(job: Job, lock_factory: Callable[[str], object] = _default_lock) -> str:
    """Run one job under its lock. Returns ``ran``, ``skipped`` or ``failed`` plus detail."""
    lock = None
    acquired = False
    try:
        lock = lock_factory(LOCK_PREFIX + job.name)
        acquired = lock.acquire()
        if not acquired:
            logger.debug("Notification job %s skipped: another worker holds its lock", job.name)
            return "skipped"
        outcome = job.run()
        logger.info("Notification job %s: %s", job.name, outcome)
        return f"ran: {outcome}"
    except Exception:  # noqa: BLE001 -- one failing job must not stop the others
        logger.exception("Notification job %s failed", job.name)
        return "failed"
    finally:
        if lock is not None and acquired:
            try:
                lock.release()
            except Exception:  # noqa: BLE001
                logger.exception("Notification job %s: could not release its lock", job.name)


def run_once(jobs: list[Job], lock_factory: Callable[[str], object] = _default_lock) -> dict[str, str]:
    """One tick: every job, one after the other."""
    return {job.name: run_job(job, lock_factory) for job in jobs}


def run_loop(
    stop: threading.Event,
    jobs: list[Job],
    interval_seconds: int,
    lock_factory: Callable[[str], object] = _default_lock,
) -> None:
    while not stop.wait(interval_seconds):
        run_once(jobs, lock_factory)


def start(stop: threading.Event, settings, jobs: list[Job]) -> threading.Thread | None:
    """Start the timer thread, or return ``None`` when it is switched off or has nothing to do."""
    if not settings.notification_scheduler_enabled or not jobs:
        return None
    interval = settings.notification_scheduler_interval_seconds
    if interval < 5:
        logger.error("Notification scheduler NOT started: NOTIFICATION_SCHEDULER_INTERVAL_SECONDS must be at least 5, got %s", interval)
        return None
    thread = threading.Thread(
        target=run_loop, args=(stop, jobs, interval), name="notification-scheduler", daemon=True
    )
    thread.start()
    logger.info("Notification scheduler on: %s, every %ds", ", ".join(job.name for job in jobs), interval)
    return thread

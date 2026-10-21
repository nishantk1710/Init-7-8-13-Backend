"""Nightly trigger for the I07 pipeline: asks the existing change check, once a night.

I07 already knows *whether* its data changed (``watch.should_run``) and how to
run itself safely (``pipeline.run_pipeline``: cross-instance lock, run record,
stop at the first failure). What nothing did was *ask*. This is the asker, and
it is deliberately thin -- it decides **when** to ask, never **whether** to run
(that stays ``watch.check_and_run``) and never **how** (the orchestrator).

Off by default (``I7_PIPELINE_WATCH_ENABLED``). Enabling it does not start a
run at start-up: a run starts only inside an open slot, and only if the data
changed and no run has started in that slot already.

Slots
-----
The nightly hour ``H`` (UTC) opens slot 0. Each retry adds a slot two hours
later: ``H, H+2, H+4, ...`` -- ``I7_PIPELINE_RETRIES`` extra slots. A slot is
open for :data:`SLOT_OPEN_MINUTES` after it starts, and the check interval is
validated to be short enough that every slot sees several ticks. A slot only
controls when a run may **start**; a run already going is never stopped by a
slot closing.

Why the state is in the database, not in memory
-----------------------------------------------
Two App Service instances can both be ticking, and a restart (or a deploy)
loses a process's memory. So nothing here is held in a variable: "has a run
started in this slot", and "how many times has this data already been tried"
are both read from ``i7_pipeline_run``. Whoever loses the race finds the row
and stands down; if both slip through, the orchestrator's ``sp_getapplock``
lock admits exactly one and the other gets ``PipelineBusy``.

Retries
-------
An attempt that did not succeed -- ``failed``, ``abandoned`` (killed by a
restart), or still ``running`` (a killed run is only marked ``abandoned`` by
the next lock-holder) -- counts against the data version it ran on (the source
fingerprint). After ``1 + retries`` such attempts that data is not tried again
by the timer: a permanent fault must not run all night. New data is a new
version and starts fresh. A manual run for the same data counts too, which
errs on the side of fewer automatic runs.

First night
-----------
Pipeline runs started by hand through the SSH helper scripts bypass the
orchestrator and leave no ``i7_pipeline_run`` row. With no succeeded run on
record, ``should_run`` answers "no successful pipeline run has processed this
data yet", so the first enabled night runs the whole pipeline once. That is
the intended, tested behaviour: it is recorded properly and proves the
automation end to end.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.i7_pipeline import STATUS_SUCCEEDED, PipelineRun

logger = get_logger(__name__)

#: Hours between a slot and its retry slot.
SLOT_SPACING_HOURS = 2

#: Minutes a slot stays open once it starts.
SLOT_OPEN_MINUTES = 60

#: Ceiling on the check interval, so a slot (``SLOT_OPEN_MINUTES``) always sees
#: at least two ticks.
MAX_INTERVAL_SECONDS = (SLOT_OPEN_MINUTES * 60) // 2

MIN_INTERVAL_SECONDS = 5

#: ``i7_pipeline_retries`` upper bound: the last slot must still fall within
#: the same night.
MAX_RETRIES = 4

TRIGGER_REASON = "nightly"


class InvalidScheduleError(ValueError):
    """The scheduler settings cannot be used. The timer does not start."""


@dataclass(frozen=True)
class SchedulePolicy:
    hour_utc: int
    retries: int
    interval_seconds: int
    initial_delay_seconds: int
    snapshots_enabled: bool = False
    """Also save the monthly KPI snapshots after the night's run (``I7_KPI_SNAPSHOT_ENABLED``)."""

    @classmethod
    def from_settings(cls, settings) -> "SchedulePolicy":
        """Read and validate the settings. Raises :class:`InvalidScheduleError`
        rather than guessing a value for one that is out of range."""
        hour = settings.i7_pipeline_nightly_hour_utc
        retries = settings.i7_pipeline_retries
        interval = settings.i7_pipeline_check_interval_seconds
        delay = settings.i7_pipeline_initial_delay_seconds
        problems = []
        if not 0 <= hour <= 23:
            problems.append(f"I7_PIPELINE_NIGHTLY_HOUR_UTC must be 0-23, got {hour}")
        if not 0 <= retries <= MAX_RETRIES:
            problems.append(f"I7_PIPELINE_RETRIES must be 0-{MAX_RETRIES}, got {retries}")
        if not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
            problems.append(
                f"I7_PIPELINE_CHECK_INTERVAL_SECONDS must be {MIN_INTERVAL_SECONDS}-"
                f"{MAX_INTERVAL_SECONDS}, got {interval}"
            )
        if delay < 0:
            problems.append(f"I7_PIPELINE_INITIAL_DELAY_SECONDS must be >= 0, got {delay}")
        if problems:
            raise InvalidScheduleError("; ".join(problems))
        return cls(hour, retries, interval, delay, bool(getattr(settings, "i7_kpi_snapshot_enabled", False)))


def open_slot(now: datetime, policy: SchedulePolicy) -> datetime | None:
    """Start of the slot open at ``now`` (UTC), or ``None``.

    Yesterday's slots are checked too, because a late slot can run past
    midnight (hour 22 with retries reaches 02:00).
    """
    if now.tzinfo is None:
        raise ValueError("open_slot needs a timezone-aware UTC time")
    today = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for days_back in (0, 1):
        base = today - timedelta(days=days_back)
        for attempt in range(policy.retries + 1):
            start = base + timedelta(hours=policy.hour_utc + attempt * SLOT_SPACING_HOURS)
            if start <= now < start + timedelta(minutes=SLOT_OPEN_MINUTES):
                return start
    return None


def runs_started_since(session: Session, since: datetime) -> int:
    """Pipeline runs of any kind that started at or after ``since``."""
    return session.execute(
        select(func.count()).select_from(PipelineRun).where(PipelineRun.started_at >= since)
    ).scalar_one()


def attempts_used(session: Session, fingerprint: str) -> int:
    """Runs for this exact data version that did not succeed."""
    return session.execute(
        select(func.count())
        .select_from(PipelineRun)
        .where(
            PipelineRun.source_fingerprint == fingerprint,
            PipelineRun.status != STATUS_SUCCEEDED,
        )
    ).scalar_one()


def capture_snapshots(session_factory: Callable[[], Session], now: datetime | None = None) -> str:
    """The nightly KPI-snapshot step: refresh this month's rolling reading and finalise last month's.

    Runs after the night's pipeline run, never during one (a reading taken mid-run would describe a half-built state
    and, being once a day, would hold until tomorrow). Idempotent and cheap when there is nothing to do: a month that is
    final, or already read today, is skipped without being recomputed.
    """
    from app.initiatives.i7.reporting import snapshots
    from app.models.i7_pipeline import STATUS_RUNNING

    today = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
    with session_factory() as session:
        running = session.execute(
            select(func.count()).select_from(PipelineRun).where(PipelineRun.status == STATUS_RUNNING)
        ).scalar_one()
        if running:
            return "a pipeline run is in progress; snapshots wait"
        results = snapshots.capture_due(session, today)
    return ", ".join(f"{r.month:%Y-%m} {r.action}" for r in results) or "up to date"


def tick(
    policy: SchedulePolicy,
    *,
    session_factory: Callable[[], Session],
    runner: Callable[[str], str],
    now: datetime | None = None,
    capture: Callable[[Callable[[], Session], datetime | None], str] | None = None,
) -> str:
    """One look. Returns a one-line description of what it did, for the log.

    Never raises: a database blip in one tick must not end the loop. ``runner``
    is ``watch.check_and_run`` in production; tests pass a fake, so nothing
    here can start a real pipeline by accident.

    When the policy asks for snapshots (and a ``capture`` step is given), they are saved after the pipeline check in
    the same open slot -- on this one timer, never a second. A failed capture is logged and reported in the returned
    line; it never ends the loop or affects the pipeline.
    """
    outcome = _tick_pipeline(policy, session_factory=session_factory, runner=runner, now=now)
    if capture is None or not policy.snapshots_enabled or outcome == "no slot open":
        return outcome
    try:
        return f"{outcome}; snapshots: {capture(session_factory, now)}"
    except Exception as exc:  # noqa: BLE001 -- see the docstring
        logger.exception("I07 nightly: the KPI snapshot capture failed (logged in i7_kpi_snapshot_log; retried next tick)")
        return f"{outcome}; snapshots failed: {type(exc).__name__}"


def _tick_pipeline(
    policy: SchedulePolicy,
    *,
    session_factory: Callable[[], Session],
    runner: Callable[[str], str],
    now: datetime | None = None,
) -> str:
    """The pipeline half of :func:`tick`: ask the change check, and run if it says so."""
    from app.initiatives.i7.watch import should_run

    now = now or datetime.now(timezone.utc)
    slot = open_slot(now, policy)
    if slot is None:
        return "no slot open"

    try:
        with session_factory() as session:
            if runs_started_since(session, slot):
                return f"a run already started in the {slot:%H:%M} slot"
            decision = should_run(session)
            if not decision.run:
                return f"no run: {decision.reason}"
            used = attempts_used(session, decision.fingerprint)
    except Exception as exc:  # noqa: BLE001 -- see the docstring
        logger.exception("I07 nightly check could not read its state")
        return f"check failed: {type(exc).__name__}: {exc}"

    limit = policy.retries + 1
    if used >= limit:
        logger.error(
            "I07 nightly: %d unsuccessful attempt(s) for this data -- retries exhausted; "
            "not trying again until the data changes or the pipeline is run by hand",
            used,
        )
        return f"retries exhausted ({used}/{limit} attempts for this data)"

    logger.info("I07 nightly: starting a run (%s; attempt %d of %d)", decision.reason, used + 1, limit)
    return runner(TRIGGER_REASON)


def run_loop(
    stop: threading.Event,
    policy: SchedulePolicy,
    *,
    session_factory: Callable[[], Session],
    runner: Callable[[str], str],
    clock: Callable[[], datetime] | None = None,
    capture: Callable[[Callable[[], Session], datetime | None], str] | None = None,
) -> None:
    """Tick every ``policy.interval_seconds`` until ``stop`` is set.

    A run, when one starts, happens inside this thread and holds it for the
    run's length -- fine, because the thread exists for nothing else, and the
    web server's event loop never waits on it. If the process stops mid-run the
    run stops with it; staging resumes from its last saved step, and the next
    open slot starts the rest.
    """
    if stop.wait(policy.initial_delay_seconds):
        return
    while not stop.is_set():
        try:
            outcome = tick(
                policy,
                session_factory=session_factory,
                runner=runner,
                now=clock() if clock else None,
                capture=capture,
            )
            logger.debug("I07 nightly tick: %s", outcome)
        except Exception:  # noqa: BLE001 -- a bad tick must never end the loop
            logger.exception("I07 nightly tick failed")
        if stop.wait(policy.interval_seconds):
            break


def start(stop: threading.Event, settings) -> threading.Thread | None:
    """Start the timer thread if it is enabled and its settings are valid.

    Returns the thread, or ``None`` when disabled or invalid (an invalid
    setting is logged and the timer stays off -- it never takes the app down).
    """
    if not settings.i7_pipeline_watch_enabled:
        return None
    try:
        policy = SchedulePolicy.from_settings(settings)
    except InvalidScheduleError as exc:
        logger.error("I07 nightly pipeline NOT started: %s", exc)
        return None

    from app.core.db import get_sessionmaker
    from app.initiatives.i7.watch import check_and_run

    thread = threading.Thread(
        target=run_loop,
        args=(stop, policy),
        kwargs={"session_factory": get_sessionmaker(), "runner": check_and_run, "capture": capture_snapshots},
        name="i7-pipeline-nightly",
        daemon=True,
    )
    thread.start()
    logger.info(
        "I07 nightly pipeline watcher on: slots from %02d:00 UTC every %dh, %d retr%s, checked every %ds",
        policy.hour_utc,
        SLOT_SPACING_HOURS,
        policy.retries,
        "y" if policy.retries == 1 else "ies",
        policy.interval_seconds,
    )
    return thread

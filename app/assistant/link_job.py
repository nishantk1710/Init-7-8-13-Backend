"""The "Link reservation" job: pull SAP, link this request, rebuild the snapshots.

A person has reserved in SAP against a request and wants the request page to show
the reservation. Four steps, in order, recorded on a ``reservation_link_job`` row
as they go so the page can show where the job is:

1. **delta** -- one delta cycle, the same one the hourly timer runs, under the
   same SQL Server lock. If a cycle is already running (the timer, a PR event,
   another press) this job waits for it and then runs its own: the running one may
   have read its windows before the reservation existed.
2. **link** -- ``session_reservation_link`` for this request's materials, from the
   reservation rows just merged (the way UAT links without a snapshot). The page
   shows the link from here on, minutes after the press, not hours.
3. **i13** -- the OAR-utilisation snapshot rebuild, which also re-links every
   request from the full data. About 78 minutes on the production extract. A build
   that started *after* this job's pull is accepted as it is; one that started before
   is waited out and then another is started, because its data predates the pull.
4. **i08** -- the repairable-spares snapshot rebuild (about 10 minutes; on the SQL
   store it queues behind the I13 build by itself).

One job runs at a time, system-wide (the ``spares_ai_reservation_link_job`` lock):
a second press for another request queues and says so. A second press for the
same request while its job is queued or running returns that job.

The job runs on a daemon thread in whichever worker took the press. The row is
the only shared state: the other worker answers the progress window from it,
and a job whose worker died (a deploy mid-build) is told from a slow one by its
heartbeat and reported ``abandoned``. The job never writes to SAP: the delta
READS SAP and merges into ``raw_<table>``; everything else is our own tables.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.reservation_link_job import ReservationLinkJob

logger = logging.getLogger(__name__)

QUEUED, RUNNING, SUCCEEDED, FAILED, ABANDONED = "queued", "running", "succeeded", "failed", "abandoned"
STEP_QUEUE, STEP_DELTA, STEP_LINK, STEP_I13, STEP_I08, STEP_DONE = "queue", "delta", "link", "i13", "i08", "done"
STEPS: tuple[tuple[str, str], ...] = (
    (STEP_QUEUE, "Waiting for a free slot"),
    (STEP_DELTA, "Pulling the latest data from SAP"),
    (STEP_LINK, "Linking this request's reservations"),
    (STEP_I13, "Rebuilding the OAR utilisation view"),
    (STEP_I08, "Rebuilding the repairable spares view"),
)
PENDING, ACTIVE, DONE, SKIPPED = "pending", "active", "done", "skipped"

LOCK_NAME = "spares_ai_reservation_link_job"
#: A running job whose heartbeat is older than this has lost its worker.
STALE_AFTER_SECONDS = 5 * 60
#: How often the runner re-checks a lock or a build it is waiting on.
POLL_SECONDS = 10
#: Longest a job waits for another delta cycle or snapshot build to finish.
MAX_WAIT_SECONDS = 3 * 3600
#: A build we asked for must show up as a run within this long.
START_GRACE_SECONDS = 120


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def new_steps() -> list[dict[str, Any]]:
    return [
        {"key": key, "label": label, "status": PENDING, "started_at": None, "finished_at": None, "detail": None}
        for key, label in STEPS
    ]


# --- what the runner talks to, replaceable in tests -------------------------------


@dataclass
class Hooks:
    """The outside world, as plain callables so the job's own logic is testable on SQLite."""

    # Delta: a lock factory whose objects have acquire() -> bool and release(), and the cycle itself.
    delta_lock: Callable[[], Any]
    run_delta: Callable[[], dict]
    # Link: reservation rows for one material-plant, and the sync that writes the links.
    reservations: Callable[[Session, str, str], list]
    sync_links: Callable[[Session, list, tuple[str, str]], Any]
    # I13: is a build running anywhere; start one; the run started at/after a moment, if any.
    i13_building: Callable[[], bool]
    i13_start: Callable[[str], bool]
    i13_run_since: Callable[[Session, datetime], tuple[str, str | None] | None]
    # I08: same three.
    i08_building: Callable[[], bool]
    i08_start: Callable[[str], bool]
    i08_run_since: Callable[[Session, datetime], tuple[str, str | None] | None]
    job_lock: Callable[[], Any]
    sleep: Callable[[float], None] = time.sleep


#: The lock the I08 build process holds (app/initiatives/i8/snapshot_store/__main__.py).
I08_BUILD_LOCK = "spares_ai_i8_snapshot_build"


def _applock_held(name: str) -> bool:
    """Whether any process holds this SQL Server application lock (the I13 build-lock probe)."""
    from sqlalchemy import text

    from app.core.db import get_engine

    try:
        with get_engine().connect() as connection:
            grantable = connection.execute(
                text("SELECT APPLOCK_TEST('public', :name, 'Exclusive', 'Session')"), {"name": name}
            ).scalar()
        return grantable == 0
    except Exception:  # noqa: BLE001 -- a probe that cannot run is "not held", and the wait loop asks again
        logger.warning("link job: could not test lock %s", name, exc_info=True)
        return False


def default_hooks() -> Hooks:
    from app.ingest import scheduler
    from app.initiatives.i13 import snapshot as i13_snapshot
    from app.initiatives.i13.session_link import sync_links
    from app.initiatives.i8 import service as i8_service
    from app.integrations.sap.postgres_reservation import fetch_reservations

    def i13_building() -> bool:
        return bool(i13_snapshot.snapshot_status().get("status") == "building")

    def i13_run_since(db: Session, moment: datetime):
        from app.models.i13_snapshot_store import I13SnapshotRun

        run = db.execute(
            select(I13SnapshotRun)
            .where(I13SnapshotRun.started_at >= moment)
            .order_by(I13SnapshotRun.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if run is None:
            return None
        return run.status, run.error

    def i08_building() -> bool:
        # This worker's build thread, or (SQL store) the build process of any worker.
        thread = i8_service.build_thread()
        return (thread is not None and thread.is_alive()) or _applock_held(I08_BUILD_LOCK)

    def i08_run_since(db: Session, moment: datetime):
        from app.models.i8_snapshot_store import I8SnapshotRun

        run = db.execute(
            select(I8SnapshotRun)
            .where(I8SnapshotRun.started_at >= moment)
            .order_by(I8SnapshotRun.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if run is None:
            return None
        return run.status, run.error

    return Hooks(
        delta_lock=scheduler._Lock,
        run_delta=scheduler.run_delta_cycle,
        reservations=lambda db, material, plant: fetch_reservations(db, material=material, plant=plant),
        sync_links=lambda db, rows, scope: sync_links(db, rows, scope=scope),
        i13_building=i13_building,
        i13_start=lambda reason: i13_snapshot.start_background_build(reason),
        i13_run_since=i13_run_since,
        i08_building=i08_building,
        i08_start=lambda reason: i8_service.start_background_build(reason, force=True),
        i08_run_since=i08_run_since,
        job_lock=lambda: scheduler._Lock(LOCK_NAME),
    )


# --- the row ----------------------------------------------------------------------


class JobFailed(Exception):
    """A step could not be completed; the message is for the person."""


def steps_of(job: ReservationLinkJob) -> list[dict[str, Any]]:
    """The job's steps as stored, decoded."""
    return json.loads(job.steps)


def latest_for_request(db: Session, request_id: str) -> ReservationLinkJob | None:
    return db.execute(
        select(ReservationLinkJob)
        .where(ReservationLinkJob.request_id == request_id)
        .order_by(ReservationLinkJob.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def is_stale(job: ReservationLinkJob, *, now: datetime | None = None) -> bool:
    """A job that says it is running but whose worker has stopped reporting."""
    if job.status not in (QUEUED, RUNNING):
        return False
    last = job.heartbeat_at or job.started_at or job.requested_at
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now or _now()) - last > timedelta(seconds=STALE_AFTER_SECONDS)


def mark_abandoned_if_stale(db: Session, job: ReservationLinkJob) -> bool:
    """Record a lost job as abandoned so the page stops waiting. Commits. True if it did."""
    if not is_stale(job):
        return False
    steps = json.loads(job.steps)
    for step in steps:
        if step["status"] == ACTIVE:
            step["status"] = SKIPPED
            step["finished_at"] = _iso(_now())
            step["detail"] = "The worker running this step stopped before it finished."
    job.steps = json.dumps(steps)
    job.status = ABANDONED
    job.finished_at = _now()
    job.error = "The job stopped before it finished (the service restarted). Press Link reservation again."
    db.commit()
    logger.warning("link job #%s for request %s abandoned: no heartbeat since %s", job.id, job.request_id, job.heartbeat_at)
    return True


def create(db: Session, request_id: str, *, requested_by: str | None) -> tuple[ReservationLinkJob, bool]:
    """The job to show for a press: the live one for this request if there is one, else a new row.

    Returns ``(job, created)``. Commits.
    """
    current = latest_for_request(db, request_id)
    if current is not None and current.status in (QUEUED, RUNNING) and not mark_abandoned_if_stale(db, current):
        return current, False
    job = ReservationLinkJob(
        request_id=request_id,
        requested_by=(requested_by or "")[:64] or None,
        status=QUEUED,
        step=STEP_QUEUE,
        steps=json.dumps(new_steps()),
        requested_at=_now(),
        heartbeat_at=_now(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job, True


# --- the runner -------------------------------------------------------------------


class _Runner:
    """Moves one job through its steps, writing the row after every change."""

    def __init__(self, job_id: int, hooks: Hooks, sessionmaker) -> None:
        self.job_id = job_id
        self.hooks = hooks
        self.sessionmaker = sessionmaker

    # -- row helpers -------------------------------------------------------------

    def _load(self, db: Session) -> ReservationLinkJob:
        job = db.get(ReservationLinkJob, self.job_id)
        if job is None:
            raise JobFailed(f"job #{self.job_id} vanished")
        return job

    def _write(self, mutate: Callable[[ReservationLinkJob, list[dict[str, Any]]], None]) -> None:
        with self.sessionmaker() as db:
            job = self._load(db)
            steps = json.loads(job.steps)
            mutate(job, steps)
            job.steps = json.dumps(steps)
            job.heartbeat_at = _now()
            db.commit()

    def _begin(self, key: str, detail: str | None = None) -> None:
        def mutate(job: ReservationLinkJob, steps: list[dict[str, Any]]) -> None:
            for step in steps:
                if step["key"] == key:
                    step["status"] = ACTIVE
                    step["started_at"] = step["started_at"] or _iso(_now())
                    step["detail"] = detail
            job.step = key
            job.status = RUNNING
            job.started_at = job.started_at or _now()

        self._write(mutate)

    def _detail(self, key: str, detail: str) -> None:
        def mutate(job: ReservationLinkJob, steps: list[dict[str, Any]]) -> None:
            for step in steps:
                if step["key"] == key:
                    step["detail"] = detail

        self._write(mutate)

    def _end(self, key: str, detail: str | None = None, *, status: str = DONE) -> None:
        def mutate(job: ReservationLinkJob, steps: list[dict[str, Any]]) -> None:
            for step in steps:
                if step["key"] == key:
                    step["status"] = status
                    step["finished_at"] = _iso(_now())
                    if detail is not None:
                        step["detail"] = detail

        self._write(mutate)

    def _heartbeat(self) -> None:
        self._write(lambda job, steps: None)

    def _cancelled(self) -> bool:
        """The row was abandoned from the outside (a stale check from another worker)."""
        with self.sessionmaker() as db:
            return self._load(db).status == ABANDONED

    # -- waiting -----------------------------------------------------------------

    def _wait_until(self, condition: Callable[[], bool], key: str, waiting_detail: str) -> None:
        """Poll ``condition`` with a heartbeat, giving up after MAX_WAIT_SECONDS."""
        started = time.monotonic()
        announced = False
        while not condition():
            if not announced:
                self._detail(key, waiting_detail)
                announced = True
            if time.monotonic() - started > MAX_WAIT_SECONDS:
                raise JobFailed(f"{waiting_detail} -- gave up after {MAX_WAIT_SECONDS // 3600} hours")
            self.hooks.sleep(POLL_SECONDS)
            self._heartbeat()
            if self._cancelled():
                raise JobFailed("the job was marked abandoned")

    # -- steps -------------------------------------------------------------------

    def _step_queue(self) -> Any:
        self._begin(STEP_QUEUE)
        lock = self.hooks.job_lock()
        self._wait_until(lock.acquire, STEP_QUEUE, "Another Link reservation job is running; this one starts when it finishes")
        self._end(STEP_QUEUE, "Started")
        return lock

    def _step_delta(self) -> datetime:
        self._begin(STEP_DELTA)
        lock = self.hooks.delta_lock()
        self._wait_until(lock.acquire, STEP_DELTA, "A SAP pull is already running; waiting for it to finish before pulling again")
        started = _now()
        self._write(lambda job, steps: setattr(job, "delta_started_at", started))
        self._detail(STEP_DELTA, "Pulling")
        try:
            summary = self.hooks.run_delta() or {}
        finally:
            lock.release()
        finished = _now()
        self._write(lambda job, steps: setattr(job, "delta_finished_at", finished))
        failed = int(summary.get("failed") or 0)
        fetched = int(summary.get("fetched") or 0)
        rows = int(summary.get("rows") or 0)
        errors = [str(e) for e in (summary.get("errors") or [])]
        if failed and not fetched:
            raise JobFailed("The SAP pull failed: " + (errors[0] if errors else "see the service log"))
        note = f"{fetched} data set(s) pulled, {rows} row(s) merged" + (f", {failed} failed" if failed else "")
        self._end(STEP_DELTA, note)
        return finished

    def _step_link(self, request_id: str) -> int:
        self._begin(STEP_LINK)
        from app.initiatives.i13.session_link import load_request_lines

        with self.sessionmaker() as db:
            lines = load_request_lines(db, [request_id]).get(request_id, [])
            pairs = sorted({(line.material, line.plant) for line in lines})
            added = kept = 0
            for material, plant in pairs:
                rows = self.hooks.reservations(db, material, plant)
                result = self.hooks.sync_links(db, rows, (material, plant))
                added += getattr(result, "added", 0)
                kept += getattr(result, "kept", 0)
            db.commit()
        total = added + kept
        self._write(lambda job, steps: setattr(job, "links", total))
        self._end(
            STEP_LINK,
            f"{total} reservation item(s) linked ({added} new)" if total else "No SAP reservation names this request yet",
        )
        return total

    def _step_build(self, key: str, since: datetime, *, building: Callable[[], bool], start: Callable[[str], bool],
                    run_since: Callable[[Session, datetime], tuple[str, str | None] | None], label: str) -> None:
        self._begin(key)

        def run_after_pull():
            with self.sessionmaker() as db:
                return run_since(db, since)

        # A build that began after our pull already has the new data: watch it instead.
        if run_after_pull() is None:
            # One that began before it does not; wait it out, then start our own.
            self._wait_until(lambda: not building(), key, f"A {label} rebuild that started before the pull is running; waiting for it")
            started = start(f"manual: link reservation ({self.job_id})")
            if not started:
                # Another process won the race in the meantime: fine if its run is after the pull.
                self._wait_until(lambda: run_after_pull() is not None or building(), key, f"Waiting for the {label} rebuild to start")
            self._detail(key, "Rebuilding")
            deadline = time.monotonic() + START_GRACE_SECONDS
            while run_after_pull() is None:
                if time.monotonic() > deadline:
                    if building():
                        break  # a memory-store build: no run row, only a status
                    raise JobFailed(f"The {label} rebuild did not start")
                self.hooks.sleep(POLL_SECONDS)
                self._heartbeat()
        else:
            self._detail(key, "Rebuilding (a rebuild with the new data was already running)")

        def finished() -> bool:
            run = run_after_pull()
            if run is not None:
                return run[0] != "building"
            return not building()

        self._wait_until(finished, key, "Rebuilding")
        run = run_after_pull()
        if run is not None and run[0] == "failed":
            raise JobFailed(f"The {label} rebuild failed: {run[1] or 'see the service log'}")
        self._end(key, "Rebuilt")

    # -- the whole job -----------------------------------------------------------

    def run(self, request_id: str) -> None:
        job_lock = None
        try:
            job_lock = self._step_queue()
            pulled = self._step_delta()
            self._step_link(request_id)
            self._step_build(STEP_I13, pulled, building=self.hooks.i13_building, start=self.hooks.i13_start,
                             run_since=self.hooks.i13_run_since, label="OAR utilisation")
            self._step_build(STEP_I08, pulled, building=self.hooks.i08_building, start=self.hooks.i08_start,
                             run_since=self.hooks.i08_run_since, label="repairable spares")
            self._write(lambda job, steps: _finish(job, SUCCEEDED, None))
        except JobFailed as exc:
            logger.warning("link job #%s for request %s failed: %s", self.job_id, request_id, exc)
            self._write(lambda job, steps: _fail(job, steps, str(exc)))
        except Exception as exc:  # noqa: BLE001 -- the row must say what happened, whatever it was
            logger.exception("link job #%s for request %s raised", self.job_id, request_id)
            self._write(lambda job, steps: _fail(job, steps, f"{type(exc).__name__}: {exc}"))
        finally:
            if job_lock is not None:
                try:
                    job_lock.release()
                except Exception:  # noqa: BLE001
                    logger.exception("link job #%s: could not release its lock", self.job_id)


def _finish(job: ReservationLinkJob, status: str, error: str | None) -> None:
    job.status = status
    job.step = STEP_DONE
    job.finished_at = _now()
    job.error = error


def _fail(job: ReservationLinkJob, steps: list[dict[str, Any]], error: str) -> None:
    for step in steps:
        if step["status"] == ACTIVE:
            step["status"] = FAILED
            step["finished_at"] = _iso(_now())
            step["detail"] = error
    _finish(job, FAILED, error)


def run_job(job_id: int, request_id: str, *, hooks: Hooks | None = None, sessionmaker=None) -> None:
    """Run one job to its end on the calling thread. Never raises."""
    if sessionmaker is None:
        from app.core.db import get_sessionmaker

        sessionmaker = get_sessionmaker()
    try:
        _Runner(job_id, hooks or default_hooks(), sessionmaker).run(request_id)
    except Exception:  # noqa: BLE001
        logger.exception("link job #%s: runner crashed", job_id)


def start_in_background(job_id: int, request_id: str) -> threading.Thread:
    """The thread the API starts after it has committed the row."""
    thread = threading.Thread(target=run_job, args=(job_id, request_id), name=f"link-job-{job_id}", daemon=True)
    thread.start()
    return thread


def enabled(settings=None) -> bool:
    settings = settings or get_settings()
    return bool(settings.link_reservation_enabled and settings.database_url)

"""The delta timer, running inside the App Service.

In-process on purpose. A Logic App, a Function timer or a Container Apps job
would each be a cleaner separation and each would be a new Azure resource to
provision, secure and pay for; the instruction was to add none. So the loop
lives in the application's own lifespan and the cost of that choice is handled
here rather than ignored.

THE COST: more than one gunicorn worker

startup.sh runs one by default, but WEB_CONCURRENCY or a scale-out adds more.
Each imports this module, so each would start a timer and every delta would run
more than once -- concurrent pulls of the same set, two loads racing into the same
table. The lock below is what stops that. It is taken in SQL Server, not in the
process, because the workers share nothing else: separate interpreters today,
and separate instances the moment this App Service scales out.

``sp_getapplock`` with ``@LockOwner='Session'`` holds for as long as the
connection lives and is released by the server if the worker dies mid-run, so a
crash cannot leave the schedule wedged forever. A lock in a table could.

WHAT IT RUNS

Deltas only, and only the ones that can RUN -- since 2026-10-07 the
purchase-order family (EKKO, EKPO, EKET, EKBE), MKPF and MSEG by entry date,
and CDHDR by change date. The other fourteen sets have no declared delta, so
running them in delta mode would make each one fall back to a full pull --
ChangeDocItemSet is 940,263 rows, and an hourly timer would have pulled all
of them, every hour, forever. Those are covered by the CSV route instead,
which is why they are skipped here rather than degraded.
``IngestSpec.runnable_delta`` is the test, and the CLI's ``--delta --all``
applies the same one.

The pass itself is ``app.ingest.sweep``, shared with the CLI: windows read
once up front, parents before children, a missing table pulled in full to
build the baseline, and each watermark advanced only after its rows are
loaded and every child read through it has succeeded too.

The CSV full pull is deliberately NOT on a timer either: it is serialised
across the whole system, it moves gigabytes, and firing one automatically while
another is open would be refused anyway. Full pulls stay a command somebody
runs on purpose.

WHAT ELSE STARTS A CYCLE

A purchase requisition SAP posts to /api/events/pr, when
DELTA_ON_PR_EVENT_ENABLED is on (``request_delta_cycle``). It runs the very
same cycle -- same gates, same sets, same lock -- after a short wait, so a burst
of requisitions shares one cycle. A cycle the lock refuses (the timer, or
another worker, is mid-run) is retried until it runs or the attempts run out:
the run in progress may have read its windows before this requisition existed.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.db import get_engine
from app.core.logging import get_logger

logger = get_logger(__name__)

# Arbitrary, stable, and unique to this job. Any other lock name is a
# different mutex, so changing it silently disables the protection.
LOCK_NAME = "spares_ai_delta_ingest"

# How long to wait for the lock before giving up for this tick. Zero, not a
# timeout: if another worker holds it, that worker is already doing the run,
# and queueing behind it would only run the same delta again the moment it
# finished.
LOCK_TIMEOUT_MS = 0


# The full refresh has its own mutex: it and the delta may run at once, and
# only a second full refresh must be kept out.
FULL_REFRESH_LOCK_NAME = "spares_ai_full_refresh"


class _Lock:
    """SQL Server application lock, held for the life of one run."""

    def __init__(self, name: str = LOCK_NAME) -> None:
        self._connection = None
        self._name = name

    def acquire(self) -> bool:
        connection = get_engine().raw_connection()
        cursor = connection.cursor()
        cursor.execute(
            "DECLARE @r int; "
            "EXEC @r = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', "
            "@LockOwner = 'Session', @LockTimeout = ?; SELECT @r;",
            self._name,
            LOCK_TIMEOUT_MS,
        )
        # 0 granted, 1 granted after waiting; negatives are refusals.
        granted = (cursor.fetchone() or [-1])[0] >= 0
        if granted:
            self._connection = connection
            return True
        with contextlib.suppress(Exception):
            connection.close()
        return False

    def release(self) -> None:
        if self._connection is None:
            return
        with contextlib.suppress(Exception):
            cursor = self._connection.cursor()
            cursor.execute(
                "EXEC sp_releaseapplock @Resource = ?, @LockOwner = 'Session';",
                self._name,
            )
        # Discard the connection rather than return it to the pool. A
        # session-owned lock lives on the connection, so if the release above
        # failed, a pooled connection would carry the lock into whatever
        # borrowed it next and the schedule would be wedged with nobody
        # holding it on purpose. Closing the connection makes the server let
        # go either way.
        with contextlib.suppress(Exception):
            self._connection.invalidate()
        with contextlib.suppress(Exception):
            self._connection.close()
        self._connection = None


def run_delta_cycle() -> dict:
    """One fetch-then-load pass over every set with a delta. Never raises.

    Synchronous and blocking -- the caller runs it in a worker thread. It talks
    to CPI and to SQL Server through drivers that are not async, and pretending
    otherwise would block the event loop and stall the API for the length of a
    pull.
    """
    from app.ingest.manifest import check_delta_filters, specs
    from app.ingest.sweep import run_delta_sweep
    from app.integrations.sap.client import SapClient

    settings = get_settings()
    root = settings.ingest_prefix
    summary = {"fetched": 0, "loaded": 0, "failed": 0, "rows": 0,
               "skipped": 0, "advanced": {}, "errors": []}

    # Before any request: a timer pointed at QA over a DEV database would
    # fetch every cycle and have every load refused. Say it once, call nothing.
    from app.ingest import sap_system

    try:
        mismatch = sap_system.verdict(sap_system.recorded(), sap_system.configured())
    except Exception as exc:
        mismatch = f"could not check which SAP system the database holds: {exc}"
    if mismatch:
        summary["errors"] = [f"delta cycle refused: {mismatch}"]
        summary["failed"] = 1
        logger.error("delta cycle refused: %s", mismatch)
        return summary

    # The same gate the CLI applies before its first request. An unverified
    # delta filter that SAP ignores returns HTTP 200 with the WHOLE set, so a
    # "delta" would quietly pull everything and merge it as an increment.
    try:
        problems = check_delta_filters()
    except Exception as exc:
        # No discovery snapshot for this CPI_PATH, most likely: the contract
        # says which one to capture.
        summary["errors"] = [f"delta cycle refused: {exc}"]
        summary["failed"] = 1
        logger.error("delta cycle refused: %s", exc)
        return summary
    if problems:
        summary["errors"] = [f"delta filters unverified: {p}" for p in problems]
        summary["failed"] = len(problems)
        logger.error(
            "delta cycle refused: %d filter(s) are not measured HONOURED. %s",
            len(problems), "; ".join(problems),
        )
        return summary

    # Runnable deltas only. See the module docstring: a set that would
    # degrade to a full pull is the CSV route's, not this timer's.
    schedulable = [s for s in specs() if s.runnable_delta is not None]
    left_out = [s for s in specs() if s.runnable_delta is None]
    summary["skipped"] = len(left_out)
    logger.info(
        "delta cycle: %d set(s) with a runnable delta (%s); %d left out",
        len(schedulable), ", ".join(s.name for s in schedulable), len(left_out),
    )
    for spec in left_out:
        if spec.delta is not None and spec.blocked:
            # Declared, and would run, but SAP cannot serve it. Worth one
            # line per cycle: it is the thing to chase, not the sets that
            # simply have no delta.
            logger.warning("%s: delta declared but %s", spec.name, spec.blocked)

    try:
        report = run_delta_sweep(
            schedulable, root=root, client=SapClient(), load=True
        )
    except Exception as exc:
        summary["failed"] = len(schedulable)
        summary["errors"] = [f"sweep raised before completing: {exc}"]
        logger.exception("delta sweep raised")
        return summary

    summary.update(report.summary())
    for name in report.baselined:
        logger.info("%s: baseline built by this cycle; incremental from the next", name)
    return summary


async def _tick(trigger: str = "timer") -> bool:
    """One delta cycle under the lock. Returns whether it ran.

    False means the lock was held elsewhere or unreachable -- the timer just
    waits for its next tick, an event retries (see ``_event_runner``).
    """
    lock = _Lock()
    try:
        if not lock.acquire():
            logger.debug("delta tick (%s) skipped: another worker holds the lock", trigger)
            return False
    except Exception as exc:
        logger.warning("delta tick (%s) skipped: could not reach the lock (%s)", trigger, exc)
        return False

    started = datetime.now(timezone.utc)
    logger.info("delta cycle starting (%s)", trigger)
    try:
        summary = await asyncio.to_thread(run_delta_cycle)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        logger.info(
            "delta cycle finished in %.0fs: %d fetched, %d loaded, %d rows, "
            "%d failed, %d skipped",
            elapsed, summary["fetched"], summary["loaded"],
            summary["rows"], summary["failed"], summary.get("skipped", 0),
        )
        for error in summary["errors"][:10]:
            logger.error("  %s", error)
    except Exception:
        logger.exception("delta cycle raised")
    finally:
        lock.release()
    return True


async def _loop(interval_seconds: int, initial_delay: int) -> None:
    # A delay before the first tick so a deploy does not start a pull while the
    # instance is still warming and migrations may still be running.
    await asyncio.sleep(initial_delay)
    while True:
        await _tick()
        await asyncio.sleep(interval_seconds)


# --- A cycle on request: the PR event ---------------------------------------
#
# One runner per process. Events that arrive while it waits ride the cycle it
# is about to run; an event that arrives while the cycle RUNS asks for one more,
# because that cycle may already have fetched the sets the new requisition
# touches. Per process, not per system: several workers each run their own,
# and the SQL Server lock keeps their cycles from overlapping.

# Results of request_delta_cycle(), also returned to SAP in the event's reply.
EVENT_DISABLED = "disabled"        # DELTA_ON_PR_EVENT_ENABLED is off
EVENT_UNAVAILABLE = "unavailable"  # no database or storage to run against
EVENT_SCHEDULED = "scheduled"      # a cycle will start after the debounce
EVENT_QUEUED = "queued"            # a cycle is already pending; this event rides it

# A cycle the lock refuses is retried this often, this many times: long enough
# to outlast a cycle in progress, bounded so a wedged lock cannot keep a task
# alive forever.
EVENT_RETRY_SECONDS = 60
EVENT_MAX_ATTEMPTS = 30

_event_task: asyncio.Task | None = None
_event_again = False


def request_delta_cycle(reason: str) -> str:
    """Ask for a delta cycle soon. Never raises, never blocks.

    Called from the PR event endpoint, inside the running event loop. Returns
    one of the EVENT_* states, which the endpoint reports back to SAP.
    """
    global _event_task, _event_again
    try:
        settings = get_settings()
        if not settings.delta_on_pr_event_enabled:
            return EVENT_DISABLED
        if not settings.database_url or not settings.storage_url:
            logger.warning("delta on %s not run: DATABASE_URL or STORAGE_URL is unset", reason)
            return EVENT_UNAVAILABLE
        if _event_task is not None and not _event_task.done():
            _event_again = True
            logger.info("delta on %s: a cycle is already pending; this event rides it", reason)
            return EVENT_QUEUED
        delay = max(settings.delta_event_debounce_seconds, 0)
        _event_again = False
        _event_task = asyncio.get_running_loop().create_task(_event_runner(reason, delay))
        logger.info("delta on %s: cycle scheduled in %ds", reason, delay)
        return EVENT_SCHEDULED
    except Exception:
        logger.exception("delta on %s could not be scheduled", reason)
        return EVENT_UNAVAILABLE


async def _event_runner(reason: str, delay: int) -> None:
    global _event_again
    attempts = 0
    wait = delay
    while True:
        await asyncio.sleep(wait)
        # Everything that arrived during the wait is covered by this cycle;
        # only what arrives from here on asks for another.
        _event_again = False
        if await _tick(trigger=f"event: {reason}"):
            if not _event_again:
                return
            reason, wait, attempts = "events during the last cycle", delay, 0
            continue
        attempts += 1
        if attempts >= EVENT_MAX_ATTEMPTS:
            logger.warning(
                "delta on %s given up after %d attempts: the lock stayed held; "
                "the next timer tick or event will pick the changes up",
                reason, attempts,
            )
            return
        wait = EVENT_RETRY_SECONDS


async def stop_event_runner() -> None:
    """Cancel a pending event cycle on shutdown."""
    global _event_task
    task, _event_task = _event_task, None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


# --- The full refresh -------------------------------------------------------
#
# The ingestion plan's backstop, once a day: every table over the CSV route,
# reconciled and loaded -- which is also the baseline the deltas merge into
# and the moment their watermarks are reset -- then the OData sets that carry
# fields the CSV extract does not (MaterialPlantSet's DISMM/PLIFZ/MINBE/MABST
# for MARC), merged into the same raw tables.
#
# A delta filter SAP silently ignores looks healthy in every log while the
# table drifts from SAP a little more each hour. The full refresh is what
# bounds that drift to one day.


def run_full_refresh_cycle() -> dict:
    """CSV sweep of every table, load, then the OData enrichment sets.

    Synchronous and blocking, like ``run_delta_cycle``; the caller runs it in
    a worker thread. Never raises.
    """
    from app.ingest import csv_load, csv_pull
    from app.ingest.fetch import MODE_FULL, fetch_set
    from app.ingest.load import STATUS_SUCCEEDED, load_set
    from app.ingest.manifest import spec_for

    settings = get_settings()
    summary = {"pulled": 0, "loaded": 0, "rows": 0, "enriched": 0, "failed": 0, "errors": []}

    # 1. Every table over the CSV route. All 21 are fired; the two SAP does
    #    not deliver time out and are reported, and the other 19 land.
    for result in csv_pull.pull_all():
        if result.ok:
            summary["pulled"] += 1
        else:
            summary["failed"] += 1
            summary["errors"].append(f"{result.sap_table}: {result.status} -- {result.error or ''}")

    # 2. Into Azure SQL. Only reconciled extracts load; each load rebuilds the
    #    normalise view over its table and resets the delta watermark.
    for result in csv_load.load_all():
        if result.ok:
            summary["loaded"] += 1
            summary["rows"] += result.rows
        else:
            summary["failed"] += 1
            summary["errors"].append(f"{result.sap_table}: load {result.status} -- {result.error or ''}")

    # 3. OData sets merged into raw_<table> for the fields CSV lacks.
    for name in settings.full_refresh_odata_set_list:
        try:
            spec = spec_for(name)
            fetched = fetch_set(spec, root=settings.ingest_prefix, mode=MODE_FULL, advance_watermark=False)
            if not fetched.ok:
                raise RuntimeError(fetched.error or "fetch unstable")
            loaded = load_set(spec, root=settings.ingest_prefix, prefix=fetched.prefix)
            if loaded.status != STATUS_SUCCEEDED:
                raise RuntimeError(loaded.error or loaded.status)
            summary["enriched"] += 1
            logger.info("%s: %s", name, loaded.raw or "loaded")
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(f"{name}: {exc}")
            logger.error("%s: enrichment failed -- %s", name, exc)

    return summary


async def _full_refresh_tick() -> None:
    lock = _Lock(FULL_REFRESH_LOCK_NAME)
    try:
        if not lock.acquire():
            logger.debug("full refresh skipped: another worker holds the lock")
            return
    except Exception as exc:
        logger.warning("full refresh skipped: could not reach the lock (%s)", exc)
        return

    started = datetime.now(timezone.utc)
    logger.info("full refresh starting")
    try:
        summary = await asyncio.to_thread(run_full_refresh_cycle)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        logger.info(
            "full refresh finished in %.0fs: %d pulled, %d loaded, %d rows, "
            "%d enriched, %d failed",
            elapsed, summary["pulled"], summary["loaded"], summary["rows"],
            summary["enriched"], summary["failed"],
        )
        for error in summary["errors"][:10]:
            logger.error("  %s", error)
    except Exception:
        logger.exception("full refresh raised")
    finally:
        lock.release()


def seconds_until(hour_utc: int, now: datetime) -> float:
    """Seconds from ``now`` to the next occurrence of ``hour_utc``:00 UTC."""
    target = now.replace(hour=hour_utc % 24, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _full_refresh_loop(hour_utc: int) -> None:
    while True:
        await asyncio.sleep(seconds_until(hour_utc, datetime.now(timezone.utc)))
        await _full_refresh_tick()
        # Past the top of the hour before recomputing, so a fast tick cannot
        # fire twice in the same minute.
        await asyncio.sleep(120)


async def _supervise(loop_factories) -> None:
    # Coroutines are created here, inside the task, so a start that is refused
    # or a task cancelled before it runs never leaves one un-awaited.
    await asyncio.gather(*(factory() for factory in loop_factories))


def start(app) -> asyncio.Task | None:
    """Start whichever timers are switched on. Returns the task, or None."""
    settings = get_settings()
    loops = []

    if settings.delta_schedule_enabled:
        interval = max(settings.delta_interval_minutes, 5) * 60
        loops.append(lambda: _loop(interval, settings.delta_initial_delay_seconds))
        logger.info(
            "delta scheduler on: every %d minute(s), first run in %ds",
            settings.delta_interval_minutes, settings.delta_initial_delay_seconds,
        )
    else:
        logger.info(
            "delta scheduler is off (DELTA_SCHEDULE_ENABLED is not 'true'); "
            "run deltas with: python -m app.ingest --fetch --load --delta --all"
        )

    if settings.delta_on_pr_event_enabled:
        logger.info(
            "delta on PR event on: a requisition posted to /api/events/pr starts a "
            "delta cycle after %ds", settings.delta_event_debounce_seconds,
        )

    if settings.full_refresh_enabled:
        loops.append(lambda: _full_refresh_loop(settings.full_refresh_hour_utc))
        logger.info(
            "full refresh on: daily at %02d:00 UTC (CSV sweep + load, then %s)",
            settings.full_refresh_hour_utc % 24,
            ", ".join(settings.full_refresh_odata_set_list) or "no OData enrichment",
        )
    else:
        logger.info(
            "full refresh is off (FULL_REFRESH_ENABLED is not 'true'); run it with: "
            "python -m app.ingest --csv-pull --csv-load --all"
        )

    if not loops:
        return None

    if not settings.database_url or not settings.storage_url:
        logger.error(
            "a scheduler is on but DATABASE_URL or STORAGE_URL is unset; "
            "not starting it -- it would fail on every tick"
        )
        return None

    return asyncio.create_task(_supervise(loops))

"""The delta timer, running inside the App Service.

In-process on purpose. A Logic App, a Function timer or a Container Apps job
would each be a cleaner separation and each would be a new Azure resource to
provision, secure and pay for; the instruction was to add none. So the loop
lives in the application's own lifespan and the cost of that choice is handled
here rather than ignored.

THE COST: startup.sh runs TWO gunicorn workers

Both import this module, so both would start a timer and every delta would run
twice -- two concurrent pulls of the same set, two loads racing into the same
table. The lock below is what stops that. It is taken in SQL Server, not in the
process, because the workers share nothing else: separate interpreters today,
and separate instances the moment this App Service scales out.

``sp_getapplock`` with ``@LockOwner='Session'`` holds for as long as the
connection lives and is released by the server if the worker dies mid-run, so a
crash cannot leave the schedule wedged forever. A lock in a table could.

WHAT IT RUNS

Deltas only, and only the ones that can RUN. Fifteen of the twenty-one sets
have no declared delta, so running them in delta mode makes each one fall
back to a full pull -- ChangeDocItemSet is 939,970 rows, and an hourly timer
would have pulled all of them, every hour, forever. One more has a delta
declared that cannot run (GoodsMovementItemSet: its parent has no date SAP
filters on), and it used to be pulled in full every cycle for the same
reason. All of those are covered by the CSV route instead, which is why they
are skipped here rather than degraded. ``IngestSpec.runnable_delta`` is the
test, and the CLI's ``--delta --all`` applies the same one.

The pass itself is ``app.ingest.sweep``, shared with the CLI: windows read
once up front, parents before children, a missing table pulled in full to
build the baseline, and each watermark advanced only after its rows are
loaded and every child read through it has succeeded too.

The CSV full pull is deliberately NOT on a timer either: it is serialised
across the whole system, it moves gigabytes, and firing one automatically while
another is open would be refused anyway. Full pulls stay a command somebody
runs on purpose.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

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


class _Lock:
    """SQL Server application lock, held for the life of one run."""

    def __init__(self) -> None:
        self._connection = None

    def acquire(self) -> bool:
        connection = get_engine().raw_connection()
        cursor = connection.cursor()
        cursor.execute(
            "DECLARE @r int; "
            "EXEC @r = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', "
            "@LockOwner = 'Session', @LockTimeout = ?; SELECT @r;",
            LOCK_NAME,
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
                LOCK_NAME,
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

    # The same gate the CLI applies before its first request. An unverified
    # delta filter that SAP ignores returns HTTP 200 with the WHOLE set, so a
    # "delta" would quietly pull everything and merge it as an increment.
    problems = check_delta_filters()
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


async def _tick() -> None:
    lock = _Lock()
    try:
        if not lock.acquire():
            logger.debug("delta tick skipped: another worker holds the lock")
            return
    except Exception as exc:
        logger.warning("delta tick skipped: could not reach the lock (%s)", exc)
        return

    started = datetime.now(timezone.utc)
    logger.info("delta cycle starting")
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


async def _loop(interval_seconds: int, initial_delay: int) -> None:
    # A delay before the first tick so a deploy does not start a pull while the
    # instance is still warming and migrations may still be running.
    await asyncio.sleep(initial_delay)
    while True:
        await _tick()
        await asyncio.sleep(interval_seconds)


def start(app) -> asyncio.Task | None:
    """Start the timer if it is switched on. Returns the task, or None."""
    settings = get_settings()
    if not settings.delta_schedule_enabled:
        logger.info(
            "delta scheduler is off (DELTA_SCHEDULE_ENABLED is not 'true'); "
            "run deltas with: python -m app.ingest --fetch --load --delta --all"
        )
        return None

    if not settings.database_url or not settings.storage_url:
        logger.error(
            "delta scheduler is on but DATABASE_URL or STORAGE_URL is unset; "
            "not starting it -- it would fail on every tick"
        )
        return None

    interval = max(settings.delta_interval_minutes, 5) * 60
    task = asyncio.create_task(_loop(interval, settings.delta_initial_delay_seconds))
    logger.info(
        "delta scheduler started: every %d minute(s), first run in %ds",
        settings.delta_interval_minutes, settings.delta_initial_delay_seconds,
    )
    return task

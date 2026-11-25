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

Deltas only, and only the ones that can RUN and are MEASURED on this system.
Since 2026-10-07 the purchase-order family (EKKO, EKPO, EKET, EKBE), MKPF
and MSEG by entry date, CDHDR by change date, EBAN by requisition date; since
2026-10-15 every set SAP gave a date in the October transport is declared too
(manifest.DELTAS), and each runs on a system once the delta probe has
measured its filter HONOURED there (discovery_<system>/delta_support.csv).
A set declared but not measured on this system is left out and named every
cycle, not run and not allowed to stop the others: a filter SAP does not know
answers HTTP 200 with the whole set, and one unverified declaration refusing
the whole cycle would have stopped QA's eight running deltas the day the
eleven new ones were declared on DEV. MBEW has no date and is refreshed
whole once a week instead (the OData full pull below). A set with no delta
at all would fall back to a full pull in delta mode -- ChangeDocItemSet was
940,263 rows before it had a date -- so those are skipped, not degraded.
``IngestSpec.runnable_delta`` and ``manifest.verified_deltas`` are the tests,
and the CLI's ``--delta --all`` applies the same ones.

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
    """SQL Server application lock, held for the life of one run.

    ``timeout_ms`` is how long to wait for it: zero (the default) gives up at
    once, which is right for a tick another worker is already running.
    """

    def __init__(self, name: str = LOCK_NAME, timeout_ms: int = LOCK_TIMEOUT_MS) -> None:
        self._connection = None
        self._name = name
        self._timeout_ms = timeout_ms

    def acquire(self) -> bool:
        connection = get_engine().raw_connection()
        cursor = connection.cursor()
        cursor.execute(
            "DECLARE @r int; "
            "EXEC @r = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', "
            "@LockOwner = 'Session', @LockTimeout = ?; SELECT @r;",
            self._name,
            self._timeout_ms,
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
    from app.ingest.manifest import specs, verified_deltas
    from app.ingest.sweep import run_delta_sweep
    from app.integrations.sap.client import SapClient

    settings = get_settings()
    root = settings.ingest_prefix
    summary = {"fetched": 0, "loaded": 0, "failed": 0, "rows": 0,
               "skipped": 0, "unverified": {}, "advanced": {}, "errors": []}

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

    # Runnable deltas only, and of those only the ones this system's snapshot
    # measures HONOURED -- the same gate the CLI applies before its first
    # request, per set. An unverified delta filter that SAP ignores returns
    # HTTP 200 with the WHOLE set, so a "delta" would quietly pull everything
    # and merge it as an increment: such a set is left out and named, and the
    # verified ones run. A set with no delta at all would degrade to a full
    # pull, which is the CSV route's job, not this timer's.
    try:
        runnable = [s for s in specs() if s.runnable_delta is not None]
        schedulable, unverified = verified_deltas(runnable)
    except Exception as exc:
        # No discovery snapshot for this CPI_PATH, most likely: the contract
        # says which one to capture.
        summary["errors"] = [f"delta cycle refused: {exc}"]
        summary["failed"] = 1
        logger.error("delta cycle refused: %s", exc)
        return summary
    left_out = [s for s in specs() if s.runnable_delta is None]
    summary["skipped"] = len(left_out) + len(unverified)
    summary["unverified"] = dict(unverified)
    for name, why in unverified.items():
        logger.warning(
            "%s: delta declared but not measured HONOURED on this system; left out "
            "until `python -m app.ingest.delta_probe --set %s` says otherwise -- %s",
            name, name, why,
        )
    logger.info(
        "delta cycle: %d set(s) with a verified, runnable delta (%s); %d left out",
        len(schedulable), ", ".join(s.name for s in schedulable), summary["skipped"],
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

    # The tables the OData delta cannot cover, over the CSV route, when that
    # is switched on. After the OData sets, one table at a time, each window
    # merged by key -- see app/ingest/csv_delta.py.
    if settings.csv_delta_enabled:
        from app.ingest import csv_delta

        try:
            results = csv_delta.run_csv_delta(settings=settings)
        except Exception as exc:  # noqa: BLE001
            logger.exception("CSV delta raised")
            summary["errors"].append(f"CSV delta raised before completing: {exc}")
            summary["failed"] += 1
            return summary
        csv_summary = csv_delta.summary(results)
        summary["csv_delta"] = csv_summary
        summary["loaded"] += csv_summary["merged"]
        summary["rows"] += csv_summary["rows"]
        summary["failed"] += csv_summary["failed"]
        summary["errors"].extend(csv_summary["errors"])
        logger.info(
            "CSV delta: %d merged, %d empty, %d skipped, %d failed, %d row(s) (%d inserted)",
            csv_summary["merged"], csv_summary["empty"], csv_summary["skipped"],
            csv_summary["failed"], csv_summary["rows"], csv_summary["inserted"],
        )
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
        if summary["loaded"] and get_settings().snapshot_refresh_after_delta:
            _schedule_snapshot_refresh(trigger)
    except Exception:
        logger.exception("delta cycle raised")
    finally:
        lock.release()
    return True


# --- The snapshots, told rather than left to notice -------------------------
#
# A merge that changed rows leaves an ingestion_run row, and the I13 and I08
# snapshots rebuild when their fingerprint over that table moves. Left to their
# periodic watchers, a change waits up to one check interval to be noticed; a
# cycle that loaded rows asks for the check itself, once, after the I13 store's
# settle wait (it refuses to build within ten minutes of a load). The same
# checks the watchers run, with the same gates, so this is earlier, not more.

_refresh_task: asyncio.Task | None = None


def _schedule_snapshot_refresh(trigger: str) -> None:
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        # A refresh is already waiting; it will see this cycle's rows too.
        return
    _refresh_task = asyncio.get_running_loop().create_task(_refresh_snapshots_after_delta(trigger))


async def _refresh_snapshots_after_delta(trigger: str) -> None:
    from app.initiatives.i13.snapshot_store.lifecycle import _SETTLE_SECONDS

    await asyncio.sleep(_SETTLE_SECONDS + 5)
    await asyncio.to_thread(check_snapshots, trigger)


def check_snapshots(trigger: str) -> dict[str, bool]:
    """Run the I13 and I08 fingerprint checks now. Returns what each started."""
    from app.core.db import get_sessionmaker
    from app.initiatives.i8 import service as i8_service
    from app.initiatives.i13 import snapshot as i13_snapshot

    started: dict[str, bool] = {}
    for label, check in (("I13", i13_snapshot.check_fingerprint), ("I08", i8_service.check_source_fingerprint)):
        try:
            db = get_sessionmaker()()
            try:
                started[label] = bool(check(db, min_interval_seconds=0))
            finally:
                db.close()
        except Exception:  # noqa: BLE001 -- one snapshot's failure must not cost the other its check
            logger.exception("%s snapshot check after the delta (%s) failed", label, trigger)
            started[label] = False
    logger.info(
        "snapshot checks after the delta (%s): %s",
        trigger, ", ".join(f"{k} {'rebuilding' if v else 'no rebuild needed'}" for k, v in started.items()),
    )
    return started


def seconds_until_next_slot(interval_seconds: int, now: datetime) -> float:
    """Seconds from ``now`` to the next whole multiple of the interval.

    Measured from the epoch, so every worker -- and every instance, if this
    App Service ever scales out -- computes the same slot. An hourly interval
    ticks at the top of each UTC hour.
    """
    remainder = now.timestamp() % interval_seconds
    wait = interval_seconds - remainder
    # Within a second of the boundary means this slot just fired (a fast
    # cycle, or clock jitter): the next one is a full interval away.
    return wait if wait >= 1 else wait + interval_seconds


async def _loop(interval_seconds: int, initial_delay: int) -> None:
    # A delay before the first tick so a deploy does not start a pull while the
    # instance is still warming and migrations may still be running.
    await asyncio.sleep(initial_delay)
    await _tick()
    # From here, ticks sit on the clock (seconds_until_next_slot) rather than
    # an interval after whenever the last cycle happened to end. Sleeping the
    # interval drifted -- and, worse, ran everything twice: with two workers,
    # the loser of the lock skipped its tick and slept the interval from THAT
    # moment, landing just after the winner's cycle ended and re-pulling the
    # same windows. 63 of 271 cycles between 13 and 22 Oct ran back to back
    # that way, 14% of the nonprod database's CPU. On a shared boundary the
    # loser's next slot is the winner's next slot, so it skips, not re-runs.
    # A cycle that outlives the interval makes both workers sit out the slot
    # that passed mid-run (one is busy, the other loses the lock); the next
    # boundary picks up, and an event cycle can always run sooner.
    while True:
        await asyncio.sleep(seconds_until_next_slot(interval_seconds, datetime.now(timezone.utc)))
        await _tick()


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
    """Cancel a pending event cycle, and a pending snapshot refresh, on shutdown."""
    global _event_task, _refresh_task
    pending = [t for t in (_event_task, _refresh_task) if t is not None and not t.done()]
    _event_task = _refresh_task = None
    for task in pending:
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

    # 3. OData sets merged into raw_<table> for the fields CSV lacks, then the
    #    tables no CSV extract fills at all (odata_tables: Department Codes).
    from app.ingest.odata_tables import ODATA_TABLES

    names = settings.full_refresh_odata_set_list
    names += [t.entity_set for t in ODATA_TABLES if t.entity_set not in names]
    for name in names:
        try:
            detail = _pull_whole_and_merge(name, settings.ingest_prefix)
            summary["enriched"] += 1
            logger.info("%s: %s", name, detail)
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(f"{name}: {exc}")
            logger.error("%s: enrichment failed -- %s", name, exc)

    return summary


def _pull_whole_and_merge(name: str, root: str) -> str:
    """One set read whole over OData and merged into ``raw_<table>`` by key.

    Returns the merge's one-line account; raises when the fetch or the load
    did not succeed. Never advances a watermark: a full read is not a
    measured delta position, and for a set with a date the CSV load owns
    the mark.
    """
    from app.ingest.fetch import MODE_FULL, fetch_set
    from app.ingest.load import STATUS_SUCCEEDED, load_set
    from app.ingest.manifest import spec_for

    spec = spec_for(name)
    fetched = fetch_set(spec, root=root, mode=MODE_FULL, advance_watermark=False)
    if not fetched.ok:
        raise RuntimeError(fetched.error or "fetch unstable")
    loaded = load_set(spec, root=root, prefix=fetched.prefix)
    if loaded.status != STATUS_SUCCEEDED:
        raise RuntimeError(loaded.error or loaded.status)
    return loaded.raw or "loaded"


# --- The weekly OData full pull ---------------------------------------------
#
# For a set with no date SAP filters on, the whole set read over OData and
# merged into raw_<table> by key, once a week: only rows whose values changed
# are written, new rows are added, nothing is deleted. MBEW is the case: a
# price table with no change date, and the SAP team's answer (2026-10-15) was
# a weekly complete extract. DepartmentCodeSet is the other (raw_dept_codes,
# app/ingest/odata_tables.py). The same read-and-merge as the full refresh's
# enrichment step, without the CSV sweep in front of it: these sets are small
# enough to read whole (QA, counts.csv: MaterialValuationSet about 130,000
# rows, DepartmentCodeSet 379) and a whole OData read is cheaper and better
# checked than a CSV delivery. A large set is refused at the client's read
# ceiling rather than pulled.

ODATA_FULL_PULL_LOCK_NAME = "spares_ai_odata_full_pull"

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def run_odata_full_pull_cycle(names: list[str] | None = None) -> dict:
    """Every set in ODATA_FULL_PULL_SETS, read whole and merged. Never raises.

    Synchronous and blocking, like the other cycles; the caller runs it in a
    worker thread. The same SAP-system check as the delta cycle: a merge from
    the wrong system is worse than no merge.
    """
    from app.ingest import sap_system

    settings = get_settings()
    chosen = list(names) if names is not None else settings.odata_full_pull_set_list
    summary = {"pulled": 0, "failed": 0, "errors": []}

    try:
        mismatch = sap_system.verdict(sap_system.recorded(), sap_system.configured())
    except Exception as exc:
        mismatch = f"could not check which SAP system the database holds: {exc}"
    if mismatch:
        summary["errors"] = [f"OData full pull refused: {mismatch}"]
        summary["failed"] = len(chosen)
        logger.error("OData full pull refused: %s", mismatch)
        return summary

    for name in chosen:
        try:
            detail = _pull_whole_and_merge(name, settings.ingest_prefix)
            summary["pulled"] += 1
            logger.info("%s: full pull merged -- %s", name, detail)
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(f"{name}: {exc}")
            logger.error("%s: full pull failed -- %s", name, exc)
    return summary


# How long the weekly pull waits for a running delta cycle before giving up
# for the week. A heavy cycle takes about five minutes on PRD.
FULL_PULL_DELTA_WAIT_MS = 20 * 60 * 1000


async def _odata_full_pull_tick() -> None:
    lock = _Lock(ODATA_FULL_PULL_LOCK_NAME)
    try:
        if not lock.acquire():
            logger.debug("OData full pull skipped: another worker holds the lock")
            return
    except Exception as exc:
        logger.warning("OData full pull skipped: could not reach the lock (%s)", exc)
        return

    # A set pulled whole may also have an hourly delta (MaterialPlantSet,
    # StorageLocationStockSet), and both merge into the same raw table through
    # the same staging table. Both fire on the hour, so the pull waits for any
    # running delta cycle and holds the delta's own lock while it merges; a
    # delta tick that finds it held skips that hour, as it does for another
    # worker. Waited for in a thread: sp_getapplock blocks, and the event loop
    # serves the API.
    delta_lock = _Lock(LOCK_NAME, timeout_ms=FULL_PULL_DELTA_WAIT_MS)
    try:
        got_delta_lock = await asyncio.to_thread(delta_lock.acquire)
    except Exception as exc:
        logger.warning("OData full pull skipped: could not reach the delta lock (%s)", exc)
        lock.release()
        return
    if not got_delta_lock:
        logger.warning(
            "OData full pull skipped: a delta cycle held the ingest lock for over %d minutes",
            FULL_PULL_DELTA_WAIT_MS // 60000,
        )
        lock.release()
        return

    started = datetime.now(timezone.utc)
    logger.info("OData full pull starting")
    try:
        summary = await asyncio.to_thread(run_odata_full_pull_cycle)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        logger.info(
            "OData full pull finished in %.0fs: %d pulled, %d failed",
            elapsed, summary["pulled"], summary["failed"],
        )
        for error in summary["errors"][:10]:
            logger.error("  %s", error)
    except Exception:
        logger.exception("OData full pull raised")
    finally:
        delta_lock.release()
        lock.release()


def seconds_until_weekday(weekday: int, hour_utc: int, now: datetime) -> float:
    """Seconds from ``now`` to the next ``weekday`` (0 = Monday) at ``hour_utc``:00 UTC."""
    target = now.replace(hour=hour_utc % 24, minute=0, second=0, microsecond=0)
    target += timedelta(days=(weekday % 7 - target.weekday()) % 7)
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()


async def _odata_full_pull_loop(weekday: int, hour_utc: int) -> None:
    while True:
        await asyncio.sleep(seconds_until_weekday(weekday, hour_utc, datetime.now(timezone.utc)))
        await _odata_full_pull_tick()
        # Past the top of the hour before recomputing, so a fast tick cannot
        # fire twice in the same minute.
        await asyncio.sleep(120)


# --- The daily OData refresh ------------------------------------------------
#
# Sets re-read over OData each day -- whole, or the last N months by their
# own delta date -- and merged by key, for the changes a creation-date delta
# never sees (app/ingest/refresh.py). The same shape as the weekly pull: its
# own mutex, then the delta's lock for the run, because the two merge through
# the same staging tables; a delta tick that finds the lock held skips that
# hour and catches up on the next, its marks untouched.

ODATA_REFRESH_LOCK_NAME = "spares_ai_odata_refresh"


async def _odata_refresh_tick() -> None:
    from app.ingest import refresh

    lock = _Lock(ODATA_REFRESH_LOCK_NAME)
    try:
        if not lock.acquire():
            logger.debug("OData refresh skipped: another worker holds the lock")
            return
    except Exception as exc:
        logger.warning("OData refresh skipped: could not reach the lock (%s)", exc)
        return

    delta_lock = _Lock(LOCK_NAME, timeout_ms=FULL_PULL_DELTA_WAIT_MS)
    try:
        got_delta_lock = await asyncio.to_thread(delta_lock.acquire)
    except Exception as exc:
        logger.warning("OData refresh skipped: could not reach the delta lock (%s)", exc)
        lock.release()
        return
    if not got_delta_lock:
        logger.warning(
            "OData refresh skipped: a delta cycle held the ingest lock for over %d minutes",
            FULL_PULL_DELTA_WAIT_MS // 60000,
        )
        lock.release()
        return

    started = datetime.now(timezone.utc)
    logger.info("OData refresh starting")
    summary: dict | None = None
    try:
        summary = await asyncio.to_thread(refresh.run_odata_refresh_cycle)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        logger.info(
            "OData refresh finished in %.0fs: %d set(s) merged, %d failed",
            elapsed, summary["pulled"], summary["failed"],
        )
        for error in summary["errors"][:10]:
            logger.error("  %s", error)
    except Exception:
        logger.exception("OData refresh raised")
    finally:
        delta_lock.release()
        lock.release()

    # Both locks are free again here: the wait below holds neither, so the
    # hourly delta runs on time while a retry is pending.
    await _odata_refresh_retry(summary)


async def _odata_refresh_retry(summary: dict | None) -> None:
    """Re-run, once, the sets that failed in the night's refresh, after
    ODATA_REFRESH_RETRY_MINUTES. Nothing happens when that is 0 (the default)
    or nothing failed. A set the setting spelt wrongly is never retried: it
    would fail the same way."""
    from app.ingest import refresh

    minutes = get_settings().odata_refresh_retry_minutes
    if minutes <= 0 or not summary or not summary.get("failed"):
        return
    failed = {s.get("name") for s in summary.get("sets", []) if s.get("error")}
    entries = [e for e in refresh.parse_sets(get_settings().odata_refresh_sets) if e.name in failed and not e.error]
    if not entries:
        return
    logger.warning(
        "OData refresh: retrying %d failed set(s) in %d minute(s): %s",
        len(entries), minutes, ", ".join(e.name for e in entries),
    )
    await asyncio.sleep(minutes * 60)

    held = await _acquire_with_delta(ODATA_REFRESH_LOCK_NAME, "OData refresh retry")
    if held is None:
        return
    lock, delta_lock = held
    started = datetime.now(timezone.utc)
    try:
        retried = await asyncio.to_thread(refresh.run_odata_refresh_cycle, entries)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        logger.info(
            "OData refresh retry finished in %.0fs: %d set(s) merged, %d failed",
            elapsed, retried["pulled"], retried["failed"],
        )
        for error in retried["errors"][:10]:
            logger.error("  %s", error)
    except Exception:
        logger.exception("OData refresh retry raised")
    finally:
        delta_lock.release()
        lock.release()


async def _acquire_with_delta(name: str, label: str) -> tuple[_Lock, _Lock] | None:
    """A job's own mutex, then the delta's lock -- the shape the weekly pull and
    the refresh use, for a job that merges through the same staging tables.
    Returns both locks, held, or None (and releases what it took) when either
    cannot be had."""
    lock = _Lock(name)
    try:
        if not lock.acquire():
            logger.debug("%s skipped: another worker holds the lock", label)
            return None
    except Exception as exc:
        logger.warning("%s skipped: could not reach the lock (%s)", label, exc)
        return None

    delta_lock = _Lock(LOCK_NAME, timeout_ms=FULL_PULL_DELTA_WAIT_MS)
    try:
        got_delta_lock = await asyncio.to_thread(delta_lock.acquire)
    except Exception as exc:
        logger.warning("%s skipped: could not reach the delta lock (%s)", label, exc)
        lock.release()
        return None
    if not got_delta_lock:
        logger.warning(
            "%s skipped: a delta cycle held the ingest lock for over %d minutes",
            label, FULL_PULL_DELTA_WAIT_MS // 60000,
        )
        lock.release()
        return None
    return lock, delta_lock


# --- Open documents, re-read by key a few times a day ---------------------------
#
# The documents still open in our tables -- requisitions not yet ordered,
# reservations not yet issued, purchase orders still due -- re-read by key at
# OPEN_DOCS_HOURS_UTC, whatever their age (app/ingest/open_docs.py). The same
# shape as the refresh: its own mutex, then the delta's lock for the run.

OPEN_DOCS_LOCK_NAME = "spares_ai_open_docs"


def seconds_until_any(hours_utc: list[int], minute: int, now: datetime) -> float:
    """Seconds from ``now`` to the next of ``hours_utc`` at ``minute`` past, UTC."""
    minute = min(max(minute, 0), 59)
    best: datetime | None = None
    for hour in hours_utc or [0]:
        target = now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        if best is None or target < best:
            best = target
    assert best is not None
    return (best - now).total_seconds()


async def _open_docs_tick() -> None:
    from app.ingest import open_docs

    held = await _acquire_with_delta(OPEN_DOCS_LOCK_NAME, "open documents re-read")
    if held is None:
        return
    lock, delta_lock = held
    started = datetime.now(timezone.utc)
    logger.info("open documents re-read starting")
    try:
        summary = await asyncio.to_thread(open_docs.run_open_docs_cycle)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        logger.info(
            "open documents re-read finished in %.0fs: %d set(s) merged, %d failed, "
            "%d row(s) SAP no longer returns",
            elapsed, summary["pulled"], summary["failed"], summary.get("missing_in_sap", 0),
        )
        for error in summary["errors"][:10]:
            logger.error("  %s", error)
    except Exception:
        logger.exception("open documents re-read raised")
    finally:
        delta_lock.release()
        lock.release()


async def _open_docs_loop(hours_utc: list[int], minute: int) -> None:
    while True:
        await asyncio.sleep(seconds_until_any(hours_utc, minute, datetime.now(timezone.utc)))
        await _open_docs_tick()
        # Past the minute before recomputing, so a fast tick cannot fire twice.
        await asyncio.sleep(120)


# --- The sync audit, once a day -------------------------------------------------
#
# A sample of documents per set read from SAP by key and compared with
# raw_<table>, field by field (app/ingest/sync_audit.py). Read-only unless
# SYNC_AUDIT_REPAIR is on; holds the delta's lock either way, so what it
# compares does not move under it.

SYNC_AUDIT_LOCK_NAME = "spares_ai_sync_audit"


async def _sync_audit_tick() -> None:
    from app.ingest import sync_audit

    held = await _acquire_with_delta(SYNC_AUDIT_LOCK_NAME, "sync audit")
    if held is None:
        return
    lock, delta_lock = held
    started = datetime.now(timezone.utc)
    logger.info("sync audit starting")
    try:
        summary = await asyncio.to_thread(sync_audit.run_sync_audit_cycle)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        drifted = sum(1 for s in summary["sets"] if s.get("differ") or s.get("missing_here") or s.get("missing_in_sap"))
        logger.info(
            "sync audit finished in %.0fs: %d set(s) audited, %d with drift, %d failed",
            elapsed, summary["audited"], drifted, summary["failed"],
        )
        for error in summary["errors"][:10]:
            logger.error("  %s", error)
    except Exception:
        logger.exception("sync audit raised")
    finally:
        delta_lock.release()
        lock.release()


async def _sync_audit_loop(hour_utc: int, minute: int) -> None:
    while True:
        await asyncio.sleep(seconds_until_any([hour_utc % 24], minute, datetime.now(timezone.utc)))
        await _sync_audit_tick()
        await asyncio.sleep(120)


async def _odata_refresh_loop(hour_utc: int) -> None:
    while True:
        await asyncio.sleep(seconds_until(hour_utc, datetime.now(timezone.utc)))
        await _odata_refresh_tick()
        # Past the top of the hour before recomputing, so a fast tick cannot
        # fire twice in the same minute.
        await asyncio.sleep(120)


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

    if settings.odata_full_pull_enabled:
        loops.append(lambda: _odata_full_pull_loop(
            settings.odata_full_pull_weekday, settings.odata_full_pull_hour_utc
        ))
        logger.info(
            "weekly OData full pull on: %s %02d:00 UTC (%s, read whole and merged into raw_<table>)",
            WEEKDAY_NAMES[settings.odata_full_pull_weekday % 7],
            settings.odata_full_pull_hour_utc % 24,
            ", ".join(settings.odata_full_pull_set_list) or "no sets configured",
        )
    else:
        logger.info(
            "weekly OData full pull is off (ODATA_FULL_PULL_ENABLED is not 'true'); run it with: "
            "python -m app.ingest --fetch --load --full --set MaterialValuationSet "
            "(and --set DepartmentCodeSet)"
        )

    if settings.odata_refresh_enabled:
        loops.append(lambda: _odata_refresh_loop(settings.odata_refresh_hour_utc))
        logger.info(
            "daily OData refresh on: %02d:00 UTC (%s; merged by key into raw_<table>, "
            "open rows older than a window re-read: %s)",
            settings.odata_refresh_hour_utc % 24,
            settings.odata_refresh_sets or "no sets configured",
            "yes" if settings.odata_refresh_open_rows else "no",
        )
    else:
        logger.info(
            "daily OData refresh is off (ODATA_REFRESH_ENABLED is not 'true'); run it with: "
            "python -m app.ingest --odata-refresh"
        )

    if settings.odata_refresh_enabled and settings.odata_refresh_retry_minutes > 0:
        logger.info(
            "daily OData refresh retry on: sets that fail are re-run once, %d minute(s) later",
            settings.odata_refresh_retry_minutes,
        )

    if settings.open_docs_enabled and settings.open_docs_hour_list:
        loops.append(lambda: _open_docs_loop(settings.open_docs_hour_list, settings.open_docs_minute))
        logger.info(
            "open documents re-read on: %s UTC (%s; open rows re-read by key and merged, "
            "rows SAP no longer returns reported)",
            ", ".join(f"{h:02d}:{min(max(settings.open_docs_minute, 0), 59):02d}" for h in settings.open_docs_hour_list),
            ", ".join(settings.open_docs_set_list) or "no sets configured",
        )
    elif settings.open_docs_enabled:
        logger.warning(
            "open documents re-read NOT started: OPEN_DOCS_HOURS_UTC=%r names no hour of the day (0-23)",
            settings.open_docs_hours_utc,
        )
    else:
        logger.info(
            "open documents re-read is off (OPEN_DOCS_ENABLED is not 'true'); run it with: "
            "python -m app.ingest --open-docs"
        )

    if settings.sync_audit_enabled:
        loops.append(lambda: _sync_audit_loop(settings.sync_audit_hour_utc, settings.sync_audit_minute))
        logger.info(
            "sync audit on: daily at %02d:%02d UTC (%d document(s) per set; %s; repair: %s)",
            settings.sync_audit_hour_utc % 24, min(max(settings.sync_audit_minute, 0), 59),
            settings.sync_audit_sample, ", ".join(settings.sync_audit_set_list) or "no sets configured",
            "yes" if settings.sync_audit_repair else "no",
        )
    else:
        logger.info(
            "sync audit is off (SYNC_AUDIT_ENABLED is not 'true'); run it with: "
            "python -m app.ingest --sync-audit"
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

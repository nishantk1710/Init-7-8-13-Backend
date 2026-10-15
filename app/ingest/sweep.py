"""One delta pass over a set of entity sets: fetch, load, then advance the marks.

Shared by the CLI (``--fetch --delta``) and the scheduler, so that both make the
same decisions in the same order. Those decisions, and why the order matters:

WINDOWS FIRST

Every direct delta's lower bound is read from the watermark table once, up
front, before anything is fetched. A parent fetched first would otherwise
advance its own mark, and its children -- resolving the mark afterwards --
would start from the new one and miss every purchase order changed between
the two. Read once, used by the parent and all of its children alike.

EACH PARENT'S KEYS READ ONCE

POHistorySet hands its purchase-order numbers to two sets (EKPO and EKET),
PurchaseOrderItemSet to two more (EKKO and EKET). The keys are collected once
per parent window and handed to every set that reads through it, rather than
read again for each one; a set with several parents gets the union.

NO BASELINE, NO INCREMENT

A delta merges into a table that already holds the set: ``raw_<table>``,
which the CSV route fills and the pages read (raw_merge puts the increment
there), or ``odata_<table>``, which an earlier OData pull built. If neither
exists, or both are EMPTY, there is nothing to merge into, and loading the
increment as if it were the table would leave one that looks complete and is
not. So such a set is pulled in full this time -- that is what builds the
baseline -- and incrementally from then on. Empty counts as missing because
that is what a wipe leaves: the 2026-10-06 reset deleted every row and kept
every table.

A CSV-loaded ``raw_<table>`` with an empty ``odata_<table>`` beside it is NOT
that case. The increment has its baseline, and ``odata_<table>`` is created
from the increment on its first load (see ``load_set``). Treating it as one
sent every set to a full OData pull -- and the four over two million rows
(EKBE, MKPF, MSEG, CDHDR) past the read ceiling, refused, every cycle.

THE MARK MOVES LAST, AND ONLY FOR A WHOLE FAMILY

``fetch_set`` is asked not to advance anything. The mark for a set's own
window is advanced here, after its rows are loaded (when this sweep loads),
and only if every set read through it also fetched and loaded. Advancing a
parent while a child failed would leave that child with no way back to the
keys it missed: the next run reads from the new mark, and the rows between
the two are never asked for again. Holding the mark costs one re-read of a
window the merge absorbs; advancing it costs rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta, timezone

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.core.storage import Storage, get_storage
from app.ingest.fetch import (
    MODE_DELTA,
    MODE_FULL,
    FetchResult,
    collect_parent_keys,
    fetch_order,
    fetch_set,
    has_window,
    resolve_windows,
)
from app.ingest.load import STATUS_SUCCEEDED, LoadResult, load_set, table_has_rows
from app.ingest.manifest import IngestSpec, spec_for
from app.ingest.raw_merge import raw_table_for
from app.ingest.watermarks import as_day, get_watermark, set_watermark
from app.integrations.sap.client import SapClient

logger = get_logger(__name__)

# A mark seeded from a load date starts this many days before it. The extract
# SAP wrote the file from was taken on or before the day it was loaded, so the
# load day alone could miss the day before. Three days of re-reading is a
# handful of rows the merge absorbs; a missed day is rows nobody asks for again.
SEED_MARGIN_DAYS = 3


def _load_day(raw_table: str) -> date | None:
    """The day the CSV route's table was last loaded, from ``ingestion_run``.

    Succeeded loads only: a failed one did not put the rows there.
    """
    from sqlalchemy import func, select

    from app.models.ingestion import IngestionRun

    with get_sessionmaker()() as session:
        latest = session.execute(
            select(func.max(IngestionRun.finished_at)).where(
                IngestionRun.target_table == raw_table, IngestionRun.status == "succeeded"
            )
        ).scalar()
    if latest is None:
        return None
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return latest.astimezone(timezone.utc).date()


def seed_marks_from_load_dates(chosen) -> dict[str, str]:
    """Give a set that has rows but no mark a starting point: the day its CSV
    table was loaded, less SEED_MARGIN_DAYS. Returns ``{set: mark}``.

    The CSV loader seeds a mark from the newest date in the file; a table that
    reached the database another way -- the 2026-10-09 migration -- has rows
    and no mark, and without one the set and every child reading through it
    is pulled in full over OData, which for EKBE, MKPF, MSEG and CDHDR is past
    the read ceiling. The load day is recorded in ``ingestion_run`` either
    way, and nothing SAP changed after the extract can carry a date before it,
    so that day, less a margin, is an honest lower bound. Written before the
    fetch, deliberately: it is a bound derived from the load, not from rows
    fetched, and a failed fetch leaves it where it is.
    """
    owners: dict[str, IngestSpec] = {}
    for spec in chosen:
        delta = spec.delta
        if delta is None:
            continue
        if delta.direct:
            owners.setdefault(spec.name, spec)
        for parent_name in delta.via:
            parent = spec_for(parent_name)
            if has_window(parent):
                owners.setdefault(parent.name, parent)

    # A table with rows and no load record at all starts from the configured
    # floor, if there is one: the operator saying which day the extract
    # behind the tables was taken. Without it, the set pulls in full.
    floor = as_day(get_settings().delta_seed_floor or None)

    seeded: dict[str, str] = {}
    for name, owner in owners.items():
        field_name = owner.delta.field or ""  # type: ignore[union-attr]
        if get_watermark(name, field_name) is not None:
            continue
        target = raw_table_for(owner)
        if target is None or not table_has_rows(target.raw_table):
            continue
        day = _load_day(target.raw_table)
        if day is not None:
            mark = (day - timedelta(days=SEED_MARGIN_DAYS)).isoformat()
            why = f"loaded on {day.isoformat()}; mark seeded at {mark} ({SEED_MARGIN_DAYS} days before)"
        elif floor is not None:
            mark = floor.isoformat()
            why = f"no load record; mark seeded at DELTA_SEED_FLOOR {mark}"
        else:
            continue
        set_watermark(name, field_name, mark, 0)
        seeded[name] = mark
        logger.info(
            "%s: no watermark, but %s holds rows, %s, so this runs as an "
            "increment, not a full pull",
            name, target.raw_table, why,
        )
    return seeded


@dataclass
class SetOutcome:
    """What happened to one entity set in this sweep."""

    name: str
    mode: str
    fetch: FetchResult | None = None
    load: LoadResult | None = None
    # A failure before the fetch could start -- the parent's keys, usually.
    error: str | None = None

    @property
    def fetched_ok(self) -> bool:
        return self.fetch is not None and self.fetch.ok

    @property
    def loaded_ok(self) -> bool:
        return self.load is not None and self.load.status == STATUS_SUCCEEDED

    @property
    def status(self) -> str:
        if self.error:
            return "FAILED"
        if self.fetch is None:
            return "FAILED"
        if not self.fetch.ok:
            return "UNSTABLE" if self.fetch.error is None else "FAILED"
        if self.load is not None and not self.loaded_ok:
            return "LOAD FAILED"
        return "ok"

    @property
    def rows(self) -> int:
        return self.fetch.rows if self.fetch else 0

    @property
    def seconds(self) -> float:
        return self.fetch.seconds if self.fetch else 0.0

    @property
    def detail(self) -> str | None:
        if self.error:
            return self.error
        if self.fetch is not None and self.fetch.error:
            return self.fetch.error
        if self.load is not None and self.load.error:
            return self.load.error
        return None


@dataclass
class SweepReport:
    outcomes: list[SetOutcome] = field(default_factory=list)
    # Direct-delta sets whose mark moved, and to what.
    advanced: dict[str, str] = field(default_factory=dict)
    # Direct-delta sets whose mark was deliberately left where it was, and why.
    held: dict[str, str] = field(default_factory=dict)
    # Sets pulled in full because their table did not exist yet.
    baselined: list[str] = field(default_factory=list)
    # Sets that had rows but no mark, and the mark seeded from their load day.
    seeded: dict[str, str] = field(default_factory=dict)

    @property
    def failures(self) -> int:
        return sum(1 for o in self.outcomes if o.status != "ok")

    def fetch_rows(self) -> list[tuple[str, str, int, float]]:
        return [(o.name, o.status if o.fetch is None or not o.fetch.ok else "ok",
                 o.rows, o.seconds) for o in self.outcomes]

    def load_rows(self) -> list[tuple[str, str, int, float]]:
        return [
            (o.name, o.load.status, o.load.rows, o.load.seconds)
            for o in self.outcomes
            if o.load is not None
        ]

    def summary(self) -> dict:
        """The scheduler's log line, as numbers."""
        errors = [f"{o.name}: {o.detail}" for o in self.outcomes if o.status != "ok"]
        errors += [f"{name}: watermark held -- {why}" for name, why in self.held.items()]
        return {
            "fetched": sum(1 for o in self.outcomes if o.fetched_ok),
            "loaded": sum(1 for o in self.outcomes if o.loaded_ok),
            "rows": sum(o.load.rows for o in self.outcomes if o.loaded_ok),
            "failed": self.failures,
            "advanced": dict(self.advanced),
            "errors": errors,
        }


def _csv_route_has_rows(spec: IngestSpec) -> bool:
    """Whether the CSV route's ``raw_<table>`` for this set holds rows.

    That table is where the increment actually goes (raw_merge) and what the
    pages read, so it is a baseline whether or not ``odata_<table>`` was ever
    built -- or was wiped, as every table was on 2026-10-06.
    """
    target = raw_table_for(spec)
    return target is not None and table_has_rows(target.raw_table)


def run_delta_sweep(
    chosen: tuple[IngestSpec, ...] | list[IngestSpec],
    *,
    root: str,
    client: SapClient | None = None,
    storage: Storage | None = None,
    since: str | None = None,
    load: bool = True,
) -> SweepReport:
    """Fetch every chosen set incrementally, load what landed, advance the marks.

    A set with no runnable delta is fetched in full by ``fetch_set``'s own
    degrade, and says so. Callers that do not want that -- the scheduler, and
    ``--delta --all`` -- filter on ``IngestSpec.runnable_delta`` first.
    """
    client = client or SapClient()
    storage = storage or get_storage()
    report = SweepReport()

    # 0. A set with rows but no mark starts from its load day. Not when the
    #    caller gave --since: that is the window, for every owner.
    if since is None:
        report.seeded = seed_marks_from_load_dates(chosen)

    # 1. Windows, once. See the module docstring.
    windows = resolve_windows(chosen, since=since)

    # 2. Baselines. A delta needs a populated table to merge into: the CSV
    #    route's raw_<table>, or odata_<table>. See the module docstring.
    forced_full: dict[str, str] = {}
    for spec in chosen:
        if spec.runnable_delta is None:
            continue
        try:
            populated = _csv_route_has_rows(spec) or table_has_rows(spec.raw_table)
        except Exception as exc:
            # Cannot tell. Treat as absent: a needless full pull is recoverable,
            # an increment merged into nothing is not.
            logger.warning("%s: could not check %s (%s); pulling in full",
                           spec.name, spec.raw_table, exc)
            populated = False
        if not populated:
            forced_full[spec.name] = f"neither the CSV route's table nor {spec.raw_table} holds rows"
            report.baselined.append(spec.name)
            logger.info(
                "%s: neither the CSV route's table nor %s holds rows, so there "
                "is nothing to merge an increment into. Pulling in full to "
                "build the baseline.",
                spec.name, spec.raw_table,
            )

    # 3. Fetch, parents first, each parent's keys collected once.
    key_cache: dict[tuple[str, str], list[str]] = {}
    for spec in fetch_order(chosen):
        mode = MODE_FULL if spec.name in forced_full else MODE_DELTA
        outcome = SetOutcome(name=spec.name, mode=mode)
        report.outcomes.append(outcome)

        window: str | None = None
        parent_keys: list[str] | None = None
        delta = spec.runnable_delta if mode == MODE_DELTA else None
        if delta is not None and delta.direct:
            window = windows.get(spec.name)
        if delta is not None and delta.derived:
            parent_windows = {p: windows.get(p) for p in delta.via}
            if all(w is not None for w in parent_windows.values()):
                union: dict[str, None] = {}
                try:
                    for parent, parent_window in parent_windows.items():
                        cache_key = (parent, delta.via_key or "")
                        if cache_key not in key_cache:
                            key_cache[cache_key] = collect_parent_keys(
                                client, parent, delta.via_key or "", parent_window
                            )
                        for key in key_cache[cache_key]:
                            union.setdefault(key, None)
                except Exception as exc:
                    outcome.error = f"could not read a parent window: {exc}"
                    logger.error("%s: %s", spec.name, outcome.error)
                    continue
                parent_keys = list(union)
                if not delta.direct:
                    # Recorded on the result as the window this set was read
                    # through; with several parents, the earliest of theirs.
                    window = min(w for w in parent_windows.values() if w is not None)
            else:
                # A parent has no mark yet: no window, no keys. Pulled in
                # full, deliberately -- reading that parent whole to collect
                # keys would be this whole set fifty keys at a time.
                mode = outcome.mode = MODE_FULL
                logger.info(
                    "%s: %s has no watermark yet, so this runs as a full pull.",
                    spec.name,
                    ", ".join(p for p, w in parent_windows.items() if w is None),
                )

        outcome.fetch = fetch_set(
            spec,
            root=root,
            client=client,
            storage=storage,
            mode=mode,
            since=window,
            parent_keys=parent_keys,
            advance_watermark=False,
        )

    # 4. Load what landed.
    if load:
        by_name = {s.name: s for s in chosen}
        for outcome in report.outcomes:
            if not outcome.fetched_ok:
                continue
            outcome.load = load_set(
                by_name[outcome.name], root=root, prefix=outcome.fetch.prefix
            )

    # 5. Marks, last, per family.
    by_name = {s.name: s for s in chosen}
    outcomes = {o.name: o for o in report.outcomes}

    def settled(outcome: SetOutcome) -> bool:
        return outcome.fetched_ok and (outcome.loaded_ok or not load)

    for spec in chosen:
        delta = spec.delta
        if delta is None or not delta.direct:
            continue
        outcome = outcomes.get(spec.name)
        if outcome is None or outcome.fetch is None:
            continue

        if not settled(outcome):
            report.held[spec.name] = (
                f"its own {'load' if outcome.fetched_ok else 'fetch'} did not succeed"
            )
            continue

        unsettled = [
            o.name for o in report.outcomes
            if o.name != spec.name
            and (child := by_name[o.name].delta) is not None
            and spec.name in child.via
            and not settled(o)
        ]
        if unsettled:
            report.held[spec.name] = (
                f"{', '.join(unsettled)} read through it and did not succeed; "
                "advancing would leave them no way back to this window"
            )
            logger.error("%s: watermark held -- %s", spec.name, report.held[spec.name])
            continue

        mark = outcome.fetch.watermark
        if not mark:
            # No rows, or none carrying the field: nothing was measured, and
            # the next run re-reads the same window, which is cheap.
            continue
        set_watermark(spec.name, delta.field or "", mark, outcome.fetch.rows)
        report.advanced[spec.name] = mark

    return report

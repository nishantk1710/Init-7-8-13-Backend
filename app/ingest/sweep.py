"""One delta pass over a set of entity sets: fetch, load, then advance the marks.

Shared by the CLI (``--fetch --delta``) and the scheduler, so that both make the
same decisions in the same order. Those decisions, and why the order matters:

WINDOWS FIRST

Every direct delta's lower bound is read from the watermark table once, up
front, before anything is fetched. A parent fetched first would otherwise
advance its own mark, and its children -- resolving the mark afterwards --
would start from the new one and miss every purchase order changed between
the two. Read once, used by the parent and all of its children alike.

PARENTS BEFORE CHILDREN, AND EACH PARENT READ ONCE

PurchaseOrderSet has three children that are read through its keys. The keys
are collected once per parent window and handed to each child, rather than
read again for every one of them.

NO BASELINE, NO INCREMENT

A delta merges into an existing ``odata_<table>``. If that table does not
exist there is nothing to merge into, and loading the increment as if it were
the table would leave one that looks complete and is not. So a set whose
table is missing is pulled in full this time -- that is what builds the
baseline -- and incrementally from then on.

THE MARK MOVES LAST, AND ONLY FOR A WHOLE FAMILY

``fetch_set`` is asked not to advance anything. The mark for a direct delta
is advanced here, after its rows are loaded (when this sweep loads), and only
if every child read through it also fetched and loaded. Advancing the parent
while a child failed would leave that child with no way back to the window it
missed: the next run reads from the new mark, and the rows between the two
are never asked for again. Holding the mark costs one re-read of a window
the merge absorbs; advancing it costs rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.core.storage import Storage, get_storage
from app.ingest.fetch import (
    MODE_DELTA,
    MODE_FULL,
    FetchResult,
    collect_parent_keys,
    fetch_order,
    fetch_set,
    resolve_windows,
)
from app.ingest.load import STATUS_SUCCEEDED, LoadResult, load_set, table_exists
from app.ingest.manifest import IngestSpec
from app.ingest.watermarks import set_watermark
from app.integrations.sap.client import SapClient

logger = get_logger(__name__)


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

    # 1. Windows, once. See the module docstring.
    windows = resolve_windows(chosen, since=since)

    # 2. Baselines. A delta needs a table to merge into.
    forced_full: dict[str, str] = {}
    for spec in chosen:
        if spec.runnable_delta is None:
            continue
        try:
            present = table_exists(spec.raw_table)
        except Exception as exc:
            # Cannot tell. Treat as absent: a needless full pull is recoverable,
            # an increment merged into nothing is not.
            logger.warning("%s: could not check for %s (%s); pulling in full",
                           spec.name, spec.raw_table, exc)
            present = False
        if not present:
            forced_full[spec.name] = f"{spec.raw_table} does not exist yet"
            report.baselined.append(spec.name)
            logger.info(
                "%s: %s does not exist, so there is nothing to merge an "
                "increment into. Pulling in full to build the baseline.",
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
        if delta is not None and delta.field is not None:
            window = windows.get(spec.name)
        elif delta is not None:
            window = windows.get(delta.via or "")
            if window is not None:
                cache_key = (delta.via or "", delta.via_key or "")
                if cache_key not in key_cache:
                    try:
                        key_cache[cache_key] = collect_parent_keys(client, delta, window)
                    except Exception as exc:
                        outcome.error = f"could not read parent {delta.via}: {exc}"
                        logger.error("%s: %s", spec.name, outcome.error)
                        continue
                parent_keys = key_cache[cache_key]
            # window None: the parent has no mark yet. fetch_set sees the
            # same and pulls this child in full; nothing to collect.

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
        if delta is None or delta.field is None:
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
            and child.via == spec.name
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
        set_watermark(spec.name, delta.field, mark, outcome.fetch.rows)
        report.advanced[spec.name] = mark

    return report

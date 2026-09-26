"""Stage one: pull an entity set from CPI and land it in object storage.

Output layout, under ``INGEST_PREFIX`` inside ``STORAGE_URL``::

    odata/<service>/<EntitySet>/<YYYY-MM-DD>/data.jsonl
    odata/<service>/<EntitySet>/<YYYY-MM-DD>/_manifest.json

JSONL, one object per line, rather than CSV. CSV cannot tell a null from an
empty string and OData returns both; it also needs an agreed escaping
convention for the free-text fields. JSONL needs no dependency, streams a line
at a time, and round-trips what SAP sent without an opinion.

The run manifest is not optional bookkeeping. It carries ``$inlinecount``, the
``$orderby`` actually used, whether that ordering had to be degraded, and the
measured duplicate-key count. Those are the facts that say whether the landed
file can be trusted, and they belong next to the bytes rather than only in a
log that rotates.

FULL AND DELTA

``mode="full"`` reads the whole set. ``mode="delta"`` reads only what changed,
by whichever of the two routes ``manifest.DELTAS`` declares for that set --
directly by a date SAP will filter on, or indirectly through a parent when it
will not. A delta with no stored watermark falls back to a full pull and says
so: the first increment has nothing to be incremental from.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from app.core.logging import get_logger
from app.core.storage import Storage, get_storage
from app.ingest.manifest import Delta, IngestSpec, spec_for
from app.ingest.watermarks import get_watermark, highest, set_watermark
from app.integrations.sap.client import SapClient
from app.integrations.sap.paging import count_duplicate_keys

logger = get_logger(__name__)

DATA_FILE = "data.jsonl"
MANIFEST_FILE = "_manifest.json"

MODE_FULL = "full"
MODE_DELTA = "delta"

# Keys per request when filtering a child by its parent's keys. The filter is a
# chain of `X eq '...' or ...` in a URL query parameter, so the ceiling is URL
# length rather than anything SAP declares. Fifty keys is roughly 1.5 KB of
# filter, which leaves generous headroom under every proxy in the path.
KEY_BATCH = 50


@dataclass
class FetchResult:
    """What one fetch produced, and whether it can be trusted."""

    entity_set: str
    prefix: str
    mode: str = MODE_FULL
    rows: int = 0
    bytes_written: int = 0
    seconds: float = 0.0

    # Straight from the client's own verification. ``stable`` false means the
    # pull lost or repeated rows and the file must not be loaded.
    counted: int | None = None
    stable: bool = True
    duplicate_keys: int = 0
    order_by: tuple[str, ...] = ()
    order_by_degraded: bool = False

    # Delta bookkeeping. ``since`` is the window's lower bound, ``watermark``
    # the value the next run will start from.
    since: str | None = None
    watermark: str | None = None
    requests: int = 1

    unknown_properties: list[str] = field(default_factory=list)

    # Recorded here so the loader can create the table in a single pass.
    # Deriving it at load time would mean reading the whole file once to learn
    # its shape and again to insert it -- two downloads of the same blob.
    columns: list[str] = field(default_factory=list)

    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.stable

    @property
    def data_key(self) -> str:
        return f"{self.prefix}/{DATA_FILE}"

    @property
    def is_delta(self) -> bool:
        return self.mode == MODE_DELTA


# --- OData literals ---------------------------------------------------------


def odata_literal(value: Any, edm_type: str) -> str:
    """One value, written the way an OData v2 ``$filter`` expects it.

    Getting this wrong does not produce a clear error. A date written as a
    plain string is a type mismatch SAP reports as HTTP 500, which is
    indistinguishable from the ``$orderby`` defect we already live with.
    """
    if edm_type in ("Edm.DateTime", "Edm.DateTimeOffset"):
        moment = value
        if isinstance(moment, str):
            moment = datetime.fromisoformat(moment)
        if isinstance(moment, datetime):
            # Naive and to the second: SAP rejects a literal carrying an
            # offset, and these fields hold dates rather than instants.
            moment = moment.replace(tzinfo=None, microsecond=0)
            return f"datetime'{moment.isoformat()}'"
        return f"datetime'{moment}'"
    # Everything else travels as a string. Doubling the quote is the OData
    # escape, and SAP keys can genuinely contain one.
    return "'" + str(value).replace("'", "''") + "'"


def _edm_type(spec: IngestSpec, property_name: str) -> str:
    found = spec.entity_set.find(property_name)
    return found.type if found else "Edm.String"


def _json_default(value: Any) -> str:
    """Serialise what the envelope decoded but JSON does not know.

    The envelope turns ``Edm.DateTime`` into a real ``datetime`` and decimals
    into ``Decimal``, which is right for callers doing arithmetic and fatal for
    ``json.dumps``. ISO for moments, plain text for the rest: the raw layer is
    text either way, and both forms round-trip unambiguously.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


# --- Paths ------------------------------------------------------------------


def prefix_for(spec: IngestSpec, *, run_date: date, root: str) -> str:
    """Where this run's files live.

    Dated, so a re-fetch never overwrites yesterday's evidence and a disputed
    number can be traced to the bytes behind it.
    """
    return f"{root.strip('/')}/{spec.service}/{spec.name}/{run_date:%Y-%m-%d}"


# --- Reading ----------------------------------------------------------------


def combine(*clauses: str | None) -> str | None:
    """Join predicates with ``and``, skipping the empty ones.

    Parenthesised because a required filter and a delta window are written
    independently and either could contain an ``or`` -- unbracketed, SAP's
    precedence would quietly widen the result.
    """
    present = [c for c in clauses if c]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    return " and ".join(f"({c})" for c in present)


def _read_whole(client: SapClient, spec: IngestSpec) -> tuple[list[dict], dict, int]:
    """Every row a set will give us, honouring any predicate it demands."""
    required = spec.required_filter
    if required:
        # allow_unsupported_filter because this predicate exists to satisfy
        # SAP, not to narrow the result. Whether the service honours it or
        # ignores it, what comes back is a superset of what we need, and the
        # alternative to sending it is HTTP 400.
        logger.info("%s: required $filter=%s", spec.name, required)
        extract = client.read_all(
            spec.name, filter=required, allow_unsupported_filter=True
        )
    else:
        extract = client.read_all(spec.name, allow_unfiltered_large_set=True)
    return list(extract.rows), _verdict(extract), 1


def _read_direct(
    client: SapClient, spec: IngestSpec, delta: Delta, since: str | None
) -> tuple[list[dict], dict, int]:
    """A set that carries a date SAP will filter on."""
    if since is None:
        logger.info(
            "%s: no watermark yet, so this delta run is a full pull. "
            "The next one will be incremental.",
            spec.name,
        )
        return _read_whole(client, spec)

    literal = odata_literal(since, _edm_type(spec, delta.field or ""))
    expression = combine(spec.required_filter, f"{delta.field} ge {literal}")
    logger.info("%s: delta $filter=%s", spec.name, expression)
    extract = client.read_all(
        spec.name, filter=expression, allow_unsupported_filter=bool(spec.required_filter)
    )
    return list(extract.rows), _verdict(extract), 1


def _read_derived(
    client: SapClient,
    spec: IngestSpec,
    delta: Delta,
    since: str | None,
    parent_keys: list[str] | None,
) -> tuple[list[dict], dict, int]:
    """A set with no filterable date, read through its parent's keys."""
    if parent_keys is None:
        parent_keys = collect_parent_keys(client, delta, since)

    if not parent_keys:
        logger.info(
            "%s: parent %s returned no changed keys, so there is nothing to pull.",
            spec.name,
            delta.via,
        )
        return [], _empty_verdict(), 0

    edm = _edm_type(spec, delta.via_key or "")
    rows: list[dict] = []
    worst = _empty_verdict()
    requests = 0

    for start in range(0, len(parent_keys), KEY_BATCH):
        chunk = parent_keys[start : start + KEY_BATCH]
        clause = combine(
            spec.required_filter,
            " or ".join(
                f"{delta.via_key} eq {odata_literal(key, edm)}" for key in chunk
            ),
        )
        extract = client.read_all(
            spec.name,
            filter=clause,
            allow_unsupported_filter=bool(spec.required_filter),
        )
        rows.extend(extract.rows)
        worst = _merge_verdicts(worst, _verdict(extract))
        requests += 1

    logger.info(
        "%s: %d rows for %d %s value(s) in %d request(s)",
        spec.name,
        len(rows),
        len(parent_keys),
        delta.via_key,
        requests,
    )
    return rows, worst, requests


def has_window(parent: IngestSpec) -> bool:
    """Whether this set can be read by a date, i.e. can drive a derived delta.

    Public because the CLI asks the same question twice: once to skip
    collecting keys it cannot collect, and once so ``--list`` does not print a
    derived delta that cannot run.
    """
    return parent.delta is not None and parent.delta.field is not None


def collect_parent_keys(
    client: SapClient, delta: Delta, since: str | None
) -> list[str]:
    """Distinct values of the linking key, from the parent's own delta window.

    Exposed rather than private so a sweep can read a parent once and hand the
    same keys to each of its children. PurchaseOrderSet has three children;
    reading EKKO four times per run would be pure waste.
    """
    parent = spec_for(delta.via or "")
    parent_delta = parent.delta
    if parent_delta is None or parent_delta.field is None:
        # Callers that route through fetch_set never reach this -- it degrades
        # such a child to a full pull instead. Kept as a guard for a direct
        # call, because the alternative is silently reading the parent whole
        # and calling the result an increment.
        raise ValueError(
            f"{parent.name} is the parent of a derived delta but has no direct "
            "delta of its own, so there is no window to read it by."
        )
    if since is None:
        # The same mistake from the other side. The CLI once passed its
        # --since here unresolved, read every purchase order ever made, and
        # then asked for their items fifty keys at a time -- the whole child
        # set in sixty requests, reported as a delta.
        raise ValueError(
            f"{parent.name} has no watermark, so there is no window to read it "
            "by. A child with no window is a full pull, not a read of its "
            "whole parent."
        )

    rows, _, _ = _read_direct(client, parent, parent_delta, since)
    seen: dict[str, None] = {}
    for row in rows:
        value = row.get(delta.via_key or "")
        if value not in (None, ""):
            seen.setdefault(str(value), None)
    return list(seen)


def fetch_order(chosen) -> list[IngestSpec]:
    """Parents before the children that are read through them.

    So a parent's keys can be collected once and handed to each child, rather
    than read again for every one of them.
    """
    parents = {s.delta.via for s in chosen if s.delta and s.delta.via}
    return sorted(chosen, key=lambda s: (s.name not in parents, s.name))


def resolve_windows(chosen, *, since: str | None = None) -> dict[str, str | None]:
    """The lower bound of every direct delta this sweep will read, taken once.

    Keyed by the set that OWNS the window: a direct-delta set itself, or the
    parent a derived child is read through -- which is included even when the
    parent is not in ``chosen``, because the child still needs its window.

    Taken up front, before any fetch, on purpose. A parent fetched first
    advances its own mark; children resolving the mark afterwards would start
    from the new one and miss everything changed between the two.
    """
    windows: dict[str, str | None] = {}
    for spec in chosen:
        delta = spec.delta
        if delta is None:
            continue
        owner = spec if delta.field is not None else spec_for(delta.via or "")
        if owner.delta is None or owner.delta.field is None:
            continue
        if owner.name not in windows:
            windows[owner.name] = since or get_watermark(owner.name, owner.delta.field)
    return windows


def _verdict(extract: Any) -> dict:
    return {
        "counted": getattr(extract, "counted", None),
        "duplicate_keys": extract.duplicate_keys,
        "order_by": tuple(extract.order_by or ()),
        "order_by_degraded": bool(extract.order_by_degraded),
        "unknown_properties": list(extract.unknown_properties or []),
    }


def _empty_verdict() -> dict:
    return {
        "counted": 0,
        "duplicate_keys": 0,
        "order_by": (),
        "order_by_degraded": False,
        "unknown_properties": [],
    }


def _merge_verdicts(left: dict, right: dict) -> dict:
    """Combine per-request verdicts into one for the whole fetch.

    Pessimistic on purpose: degraded anywhere means degraded, and the counts
    add up. A chunked read is only as trustworthy as its worst chunk.
    """
    return {
        "counted": None,  # meaningless across chunks; the row count is the truth
        "duplicate_keys": left["duplicate_keys"] + right["duplicate_keys"],
        "order_by": right["order_by"] or left["order_by"],
        "order_by_degraded": left["order_by_degraded"] or right["order_by_degraded"],
        "unknown_properties": sorted(
            set(left["unknown_properties"]) | set(right["unknown_properties"])
        ),
    }


# --- The fetch ---------------------------------------------------------------


def fetch_set(
    spec: IngestSpec,
    *,
    root: str,
    client: SapClient | None = None,
    storage: Storage | None = None,
    run_date: date | None = None,
    mode: str = MODE_FULL,
    since: str | None = None,
    parent_keys: list[str] | None = None,
    advance_watermark: bool = True,
) -> FetchResult:
    """Pull one entity set and land it. Never raises -- failures are returned.

    Returned rather than raised because ``--all`` must not stop at set 7 of 21:
    one service defect should cost that set, not the sweep.
    """
    client = client or SapClient()
    storage = storage or get_storage()
    run_date = run_date or datetime.now(timezone.utc).date()
    prefix = prefix_for(spec, run_date=run_date, root=root)
    started = time.monotonic()

    delta = spec.delta if mode == MODE_DELTA else None
    if mode == MODE_DELTA and delta is None:
        logger.info(
            "%s: no delta declared, so this runs as a full pull.", spec.name
        )
        mode = MODE_FULL

    # The field this set's mark is measured on, whatever mode the read ends up
    # in. A full pull of a set that has a direct delta -- asked for, or forced
    # because there is no mark yet -- reads every row, and the highest value
    # seen is exactly where the next increment should start.
    mark_field = spec.delta.field if spec.delta is not None else None

    result = FetchResult(entity_set=spec.name, prefix=prefix, mode=mode)

    try:
        if delta is not None and delta.field is not None:
            result.since = since or get_watermark(spec.name, delta.field)
            if result.since is None:
                # The first increment has nothing to be incremental from. A
                # FULL pull, and marked as one: the file replaces the table
                # rather than merging into one that may not exist, and the
                # short-read check against $count applies.
                logger.info(
                    "%s: no watermark yet, so this delta run is a full pull. "
                    "The next one will be incremental.",
                    spec.name,
                )
                delta = None
                result.mode = mode = MODE_FULL
        elif delta is not None:
            parent = spec_for(delta.via or "")
            if parent_keys is None and not has_window(parent):
                # A derived delta's window is its parent's date window, and
                # this parent has none SAP will honour -- MKPF's Budat is
                # IGNORED, so MSEG sits here. Reading the parent in full and
                # then asking for its children 50 keys at a time would be the
                # whole child set in hundreds of requests, slower than the
                # full pull it pretends to improve on.
                logger.info(
                    "%s: parent %s has no delta of its own, so there is no "
                    "window to read it by. This runs as a full pull.",
                    spec.name,
                    delta.via,
                )
                delta = None
                result.mode = mode = MODE_FULL
            else:
                parent_field = (parent.delta.field if parent.delta else None) or ""
                result.since = since or get_watermark(parent.name, parent_field)
                if parent_keys is None and result.since is None:
                    logger.info(
                        "%s: parent %s has no watermark yet, so there is no "
                        "window to read it by. This runs as a full pull.",
                        spec.name,
                        delta.via,
                    )
                    delta = None
                    result.mode = mode = MODE_FULL

        if delta is None:
            rows, verdict, requests = _read_whole(client, spec)
        elif delta.field is not None:
            rows, verdict, requests = _read_direct(client, spec, delta, result.since)
        else:
            rows, verdict, requests = _read_derived(
                client, spec, delta, result.since, parent_keys
            )
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.seconds = time.monotonic() - started
        logger.error("%s: fetch failed -- %s", spec.name, result.error)
        return result

    result.rows = len(rows)
    result.requests = requests
    result.counted = verdict["counted"]
    result.order_by = verdict["order_by"]
    result.order_by_degraded = verdict["order_by_degraded"]
    result.unknown_properties = verdict["unknown_properties"]
    result.columns = columns_of(spec, rows)

    # Counted across the whole fetch, not per request. A chunked read can
    # return the same row from two chunks if the parent handed us a duplicate
    # key, and no single request would notice.
    #
    # identity_keys, not keys: CDPOS's declared key repeats once per field
    # changed in a document, so counting against it would condemn every correct
    # pull of that set as corrupt.
    result.duplicate_keys = count_duplicate_keys(rows, spec.identity_keys)
    result.stable = result.duplicate_keys == 0

    # A pull that returns FEWER rows than SAP's own count is a pull that lost
    # rows, and until now nothing said so: `stable` only ever asked about
    # duplicates, so a short file scored ok=True and loaded cleanly.
    #
    # POHistorySet is the live example -- it returns 0 rows against a $count of
    # 3,881 on every sweep. That would have landed an empty file, passed the
    # duplicate check perfectly, and told I13 that EKBE has no goods receipts.
    #
    # Only for a full pull. A delta returns a window by design, and its count
    # is the whole set.
    if (
        not result.is_delta
        and result.counted is not None
        and result.rows < result.counted
    ):
        result.stable = False
        result.error = (
            f"read {result.rows} row(s) against a $count of {result.counted}. "
            "Rows were lost in paging; the file would look complete and not be."
        )

    # Where the next increment would start, for a set with a date of its own.
    # A derived child was filtered by its parent's keys, so it measured no
    # position and gets none. Computed before the manifest is written so the
    # file carries it; written to the watermark table further down, and only
    # past a successful landing.
    if result.ok and mark_field is not None and rows:
        result.watermark = highest(rows, mark_field)

    try:
        result.bytes_written = _write_jsonl(storage, result.data_key, rows)
        _write_manifest(storage, prefix, spec, result, run_date)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        logger.error(
            "%s: fetched %d rows but could not land them -- %s",
            spec.name, result.rows, result.error,
        )
        return result
    finally:
        result.seconds = time.monotonic() - started

    if not result.stable:
        # Landed anyway. The file is evidence of the defect, and deleting it
        # would leave nothing to diagnose; the manifest marks it unusable and
        # the loader refuses it.
        logger.error(
            "%s: %d duplicate key(s) across %d rows -- the pull lost rows. "
            "Landed at %s and marked unstable; it must not be loaded.",
            spec.name, result.duplicate_keys, result.rows, prefix,
        )
    elif result.order_by_degraded:
        logger.warning(
            "%s: SAP refused the full key ordering; used %s. No rows lost this "
            "time, but that ordering is not unique.",
            spec.name, ", ".join(result.order_by),
        )

    # The candidate was recorded before the manifest was written (above), so
    # the file says where the next increment would start. Written to the
    # watermark table only now, past a successful landing, and only if this
    # call is trusted to. A sweep says no: it advances the mark itself, after
    # the rows are loaded and only once every child read through this set
    # has succeeded too.
    if advance_watermark and result.ok and result.watermark and mark_field:
        set_watermark(spec.name, mark_field, result.watermark, result.rows)
    elif result.watermark and mark_field:
        logger.info(
            "%s: watermark candidate %s=%s (not advanced here)",
            spec.name, mark_field, result.watermark,
        )

    logger.info(
        "%s: %d rows, %d bytes -> %s (%.1fs, %s)",
        spec.name, result.rows, result.bytes_written, result.data_key,
        result.seconds, result.mode,
    )
    return result


def columns_of(spec: IngestSpec, rows: list[dict]) -> list[str]:
    """Column order for the target table.

    Declared properties first, in the order the contract lists them, so the
    table reads the way SAP describes the entity rather than the way a dict
    happened to serialise. Anything present in the data but not in the contract
    is appended -- that is drift, and dropping it silently is how a new field
    goes unnoticed for a month.
    """
    declared = [p.name for p in spec.entity_set.properties]
    seen = set(declared)
    extra = [k for row in rows for k in row if k not in seen and not seen.add(k)]
    present = {k for row in rows for k in row}
    if extra:
        logger.warning(
            "%s: %d column(s) in the data but not in the contract: %s",
            spec.name, len(extra), ", ".join(sorted(extra)),
        )
    # Declared-but-absent columns are kept. The table then matches the contract
    # whether or not a given pull happened to return every field, which keeps
    # the shape stable across runs.
    missing = [name for name in declared if name not in present]
    if missing and rows:
        logger.info(
            "%s: %d declared column(s) absent from this pull: %s",
            spec.name, len(missing), ", ".join(missing),
        )
    return declared + extra


def _write_jsonl(storage: Storage, key: str, rows: list[dict]) -> int:
    """Write one JSON object per line. Returns bytes written.

    Encoded a row at a time rather than joined into one string: the largest set
    is modest today but the whole point of a streaming sink is not having to
    revisit this when it is not.
    """
    written = 0
    with storage.open_write(key) as sink:
        for row in rows:
            line = json.dumps(
                row, ensure_ascii=False, separators=(",", ":"), default=_json_default
            )
            encoded = (line + "\n").encode("utf-8")
            sink.write(encoded)
            written += len(encoded)
    return written


def _write_manifest(
    storage: Storage,
    prefix: str,
    spec: IngestSpec,
    result: FetchResult,
    run_date: date,
) -> None:
    payload = {
        "entity_set": spec.name,
        "service": spec.service,
        "target_table": spec.raw_table,
        "keys": list(spec.keys),
        # Provenance. These two tables hold MATERIAL-class change documents
        # rather than every change document in the client, and a reader who
        # does not know that will draw the wrong conclusion from a count.
        "required_filter": spec.required_filter,
        "run_date": run_date.isoformat(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data_file": DATA_FILE,
        **{k: v for k, v in asdict(result).items() if k != "prefix"},
        # Spelled out rather than inferred by a reader: "can this file be
        # loaded" is the question the manifest exists to answer.
        "usable": result.ok,
        # And how: a delta file merges into the table, a full one replaces it.
        "load_strategy": "merge" if result.is_delta else "replace",
    }
    body = json.dumps(payload, indent=2, default=_json_default).encode("utf-8")
    with storage.open_write(f"{prefix}/{MANIFEST_FILE}") as sink:
        sink.write(body)


def read_manifest(storage: Storage, prefix: str) -> dict:
    """The run manifest for a landed fetch."""
    with storage.open_read(f"{prefix}/{MANIFEST_FILE}") as source:
        return json.loads(source.read().decode("utf-8"))

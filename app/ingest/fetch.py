"""Stage one: pull an entity set from CPI and land it in object storage.

Output layout, under ``INGEST_PREFIX`` inside ``STORAGE_URL``::

    odata/<service>/<EntitySet>/<YYYY-MM-DD>/data.jsonl
    odata/<service>/<EntitySet>/<YYYY-MM-DD>/_manifest.json

JSONL, one object per line, rather than CSV. CSV cannot tell a null from an
empty string and OData returns both; it also needs an agreed escaping
convention for the free-text fields. JSONL needs no dependency, streams a line
at a time, and round-trips what SAP sent without an opinion.

The run manifest is not optional bookkeeping. It carries ``$count``, the
``$orderby`` actually used, whether that ordering had to be degraded, and the
measured duplicate-key count. Those are the facts that say whether the landed
file can be trusted, and they belong next to the bytes rather than only in a
log that rotates.

FULL AND DELTA

``mode="full"`` reads the whole set. ``mode="delta"`` reads only what changed,
by whichever parts ``manifest.DELTAS`` declares for that set -- its own date
window, the keys its parents' windows hand over, or both, unioned. A delta
with no stored watermark, or a parent with none, falls back to a full pull and
says so: the first increment has nothing to be incremental from.

EVERY READ IS CHECKED AGAINST ITS OWN COUNT

The client asks ``$count`` with the same filter before it pages. Fewer rows
than that count is a read that lost rows, full or delta alike: a window of 20
purchase orders that lands 19 is as wrong as a full pull that lands 3,139 of
3,140, and both used to load without a word.
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
from app.ingest.manifest import (
    LITERAL_DATETIME,
    LITERAL_DATS,
    LITERAL_DOTTED,
    IngestSpec,
    spec_for,
)
from app.ingest.watermarks import as_day, get_watermark, highest, set_watermark
from app.integrations.sap.client import SapClient
from app.integrations.sap.paging import count_duplicate_keys

logger = get_logger(__name__)

DATA_FILE = "data.jsonl"
MANIFEST_FILE = "_manifest.json"

MODE_FULL = "full"
MODE_DELTA = "delta"

# Keys per request when filtering a child by its parents' keys. The filter is a
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

    # What SAP's $count said the read(s) would return: the sum across every
    # request, or None where any one could not say. Each read is checked
    # against its own count; the sum can exceed ``rows`` for a set read by its
    # own window AND its parents' keys, because the two overlap and the union
    # keeps one copy. ``stable`` false means the pull lost or repeated rows
    # and the file must not be loaded.
    counted: int | None = None
    stable: bool = True
    duplicate_keys: int = 0
    order_by: tuple[str, ...] = ()
    order_by_degraded: bool = False

    # Delta bookkeeping. ``since`` is the lower bound of this set's own window,
    # ``parent_windows`` the bound each parent was read from, ``parent_keys``
    # how many keys they handed over, and ``watermark`` the day the next run
    # will start from.
    since: str | None = None
    parent_windows: dict[str, str | None] = field(default_factory=dict)
    parent_keys: int | None = None
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


def delta_literal(mark: Any, literal: str) -> str:
    """A watermark day, written in the shape this delta field's filter accepts.

    The shapes were measured, not inferred (manifest.LITERAL_*): the same SAP
    date takes three different literals on three different sets, and the
    wrong one is either HTTP 400 or -- worse -- HTTP 200 with the whole set.
    """
    day = as_day(mark)
    if day is None:
        raise ValueError(f"watermark {mark!r} is not a date")
    if literal == LITERAL_DATETIME:
        return f"datetime'{day.isoformat()}T00:00:00'"
    if literal == LITERAL_DATS:
        return f"'{day:%Y%m%d}'"
    if literal == LITERAL_DOTTED:
        return f"'{day:%d.%m.%Y}'"
    raise ValueError(f"unknown literal shape {literal!r}")


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


@dataclass
class _Read:
    """One logical read -- a window, a run of key batches, or the whole set --
    and the evidence that it is complete."""

    rows: list[dict] = field(default_factory=list)
    expected: int | None = 0
    duplicate_keys: int = 0
    truncated: bool = False
    order_by: tuple[str, ...] = ()
    order_by_degraded: bool = False
    unknown_properties: list[str] = field(default_factory=list)
    requests: int = 0

    def absorb(self, extract: Any) -> None:
        """Add one request's extract. Pessimistic: degraded anywhere means
        degraded, and the counts add up -- a chunked read is only as
        trustworthy as its worst chunk."""
        self.rows.extend(extract.rows)
        expected = getattr(extract, "expected", None)
        self.expected = None if self.expected is None or expected is None else self.expected + expected
        self.duplicate_keys += extract.duplicate_keys
        self.truncated = self.truncated or bool(getattr(extract, "truncated", False))
        self.order_by = tuple(extract.order_by or ()) or self.order_by
        self.order_by_degraded = self.order_by_degraded or bool(extract.order_by_degraded)
        self.unknown_properties = sorted(set(self.unknown_properties) | set(extract.unknown_properties or []))
        self.requests += 1

    def shortfall(self) -> str | None:
        """Why this read cannot be trusted to hold every row, or None."""
        if self.truncated:
            return (
                f"stopped at the safety row limit after {len(self.rows)} row(s); "
                "the file would look complete and not be."
            )
        if self.expected is not None and len(self.rows) < self.expected:
            return (
                f"read {len(self.rows)} row(s) against a $count of {self.expected}. "
                "Rows were lost in paging; the file would look complete and not be."
            )
        return None


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


def _read_whole(client: SapClient, spec: IngestSpec) -> _Read:
    """Every row a set will give us, honouring any predicate it demands."""
    required = spec.required_filter
    read = _Read()
    if required:
        # allow_unsupported_filter because this predicate exists to satisfy
        # SAP, not to narrow the result. Whether the service honours it or
        # ignores it, what comes back is a superset of what we need, and the
        # alternative to sending it is HTTP 400.
        logger.info("%s: required $filter=%s", spec.name, required)
        read.absorb(client.read_all(spec.name, filter=required, allow_unsupported_filter=True))
    else:
        read.absorb(client.read_all(spec.name, allow_unfiltered_large_set=True))
    return read


def window_filter(spec: IngestSpec, since: str) -> str:
    """``field ge <since>`` in the set's measured literal shape, plus any
    predicate the set demands."""
    delta = spec.delta
    if delta is None or not delta.direct:
        raise ValueError(f"{spec.name} has no window of its own")
    return combine(spec.required_filter, f"{delta.field} ge {delta_literal(since, delta.literal)}") or ""


def _read_direct(client: SapClient, spec: IngestSpec, since: str) -> _Read:
    """A set's own window: every row whose delta date is on or after ``since``.

    The client's own guard still runs on the window (it refuses a property
    measured IGNORED or HTTP 500), as a backstop behind check_delta_filters.
    A required predicate is let through it: that predicate exists to satisfy
    SAP, not to narrow the result.
    """
    expression = window_filter(spec, since)
    logger.info("%s: delta $filter=%s", spec.name, expression)
    read = _Read()
    read.absorb(
        client.read_all(
            spec.name, filter=expression, allow_unsupported_filter=bool(spec.required_filter)
        )
    )
    return read


def _read_keys(client: SapClient, spec: IngestSpec, keys: list[str]) -> _Read:
    """Every row of this set belonging to one of ``keys``, fifty at a time."""
    delta = spec.delta
    via_key = (delta.via_key if delta else None) or ""
    edm = _edm_type(spec, via_key)
    read = _Read()
    for start in range(0, len(keys), KEY_BATCH):
        chunk = keys[start : start + KEY_BATCH]
        clause = combine(
            spec.required_filter,
            " or ".join(f"{via_key} eq {odata_literal(key, edm)}" for key in chunk),
        )
        read.absorb(
            client.read_all(
                spec.name, filter=clause, allow_unsupported_filter=bool(spec.required_filter)
            )
        )

    # Counted across the chunks, not per request. The keys are distinct, so
    # the same row arriving from two chunks means SAP returned a row for a key
    # it was not asked for -- no single request would notice.
    read.duplicate_keys = count_duplicate_keys(read.rows, spec.identity_keys)
    logger.info(
        "%s: %d row(s) for %d %s value(s) in %d request(s)",
        spec.name, len(read.rows), len(keys), via_key, read.requests,
    )
    return read


def has_window(parent: IngestSpec) -> bool:
    """Whether this set can be read by a date, i.e. can hand keys to a child.

    Public because the CLI asks the same question twice: once to skip
    collecting keys it cannot collect, and once so ``--list`` does not print a
    derived delta that cannot run.
    """
    return parent.delta is not None and parent.delta.direct


def collect_parent_keys(
    client: SapClient, parent_name: str, via_key: str, since: str | None
) -> list[str]:
    """Distinct values of ``via_key`` in the parent's own delta window.

    Exposed rather than private so a sweep can read a parent once and hand the
    same keys to every child: POHistorySet hands keys to EKPO and EKET, and
    reading it once per child would be pure waste.
    """
    parent = spec_for(parent_name)
    if not has_window(parent):
        # Callers that route through fetch_set never reach this -- it degrades
        # such a child to a full pull instead. Kept as a guard for a direct
        # call, because the alternative is silently reading the parent whole
        # and calling the result an increment.
        raise ValueError(
            f"{parent.name} is the parent of a derived delta but has no window "
            "of its own, so there is no window to read it by."
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

    read = _read_direct(client, parent, since)
    shortfall = read.shortfall()
    if shortfall:
        # A parent window that lost rows hands over too few keys, and the
        # child would land an increment missing exactly those purchase orders.
        raise ValueError(f"{parent.name} window: {shortfall}")
    seen: dict[str, None] = {}
    for row in read.rows:
        value = row.get(via_key)
        if value not in (None, ""):
            seen.setdefault(str(value), None)
    return list(seen)


def fetch_order(chosen) -> list[IngestSpec]:
    """Parents before the sets that read through them.

    Not needed for correctness -- keys come from each parent's own window,
    read separately -- but it puts the windows that decide everything else at
    the top of the log.
    """
    parents = {p for s in chosen if s.delta for p in s.delta.via}
    return sorted(chosen, key=lambda s: (s.name not in parents, s.name))


def resolve_windows(chosen, *, since: str | None = None) -> dict[str, str | None]:
    """The lower bound of every window this sweep will read, taken once.

    Keyed by the set that OWNS the window: each chosen set with a date of its
    own, and every parent a chosen set reads through -- included even when
    the parent is not in ``chosen``, because the child still needs its keys.

    Taken up front, before any fetch, on purpose. A parent fetched first
    advances its own mark; children resolving the mark afterwards would start
    from the new one and miss everything changed between the two.
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

    windows: dict[str, str | None] = {}
    for name, owner in owners.items():
        mark = since or get_watermark(name, owner.delta.field or "")  # type: ignore[union-attr]
        day = as_day(mark)
        windows[name] = day.isoformat() if day else None
    return windows


def _union(spec: IngestSpec, *reads: _Read) -> list[dict]:
    """Rows from several reads, one per identity key.

    The parts overlap by design -- an item changed today is in EKPO's own
    window AND in the purchase order its new goods receipt hands over -- so
    a row read twice is the same row, not a paging loss. Later reads win;
    they are seconds apart.
    """
    keys = spec.identity_keys
    merged: dict[tuple, dict] = {}
    keyless: list[dict] = []
    for read in reads:
        for row in read.rows:
            identity = tuple(row.get(k) for k in keys)
            if not keys or None in identity:
                keyless.append(row)
            else:
                merged[identity] = row
    return list(merged.values()) + keyless


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

    In delta mode, ``since`` is the lower bound of the set's own window (or,
    for a set with only parents, of every parent's); ``parent_keys`` is the
    union of the keys the parents' windows handed over, when the caller has
    already read them -- a sweep does, once per parent. Left None, both are
    resolved here from the watermark table.
    """
    client = client or SapClient()
    storage = storage or get_storage()
    run_date = run_date or datetime.now(timezone.utc).date()
    prefix = prefix_for(spec, run_date=run_date, root=root)
    started = time.monotonic()

    delta = spec.delta if mode == MODE_DELTA else None
    if mode == MODE_DELTA and delta is None:
        logger.info("%s: no delta declared, so this runs as a full pull.", spec.name)
        mode = MODE_FULL

    # The field this set's mark is measured on, whatever mode the read ends up
    # in. A full pull of a set that has a date of its own -- asked for, or
    # forced because there is no mark yet -- reads every row, and the latest
    # day seen is exactly where the next increment should start.
    mark_field = spec.delta.field if spec.delta is not None else None

    result = FetchResult(entity_set=spec.name, prefix=prefix, mode=mode)

    def go_full(reason: str) -> None:
        # The first increment has nothing to be incremental from. A FULL pull,
        # and marked as one: the file replaces the table rather than merging
        # into one that may not exist, and the short-read check applies.
        nonlocal delta, mode
        logger.info("%s: %s, so this delta run is a full pull.", spec.name, reason)
        delta = None
        result.mode = mode = MODE_FULL

    try:
        if delta is not None and delta.direct:
            result.since = since or get_watermark(spec.name, delta.field or "")
            if result.since is None:
                go_full("no watermark yet")
        elif delta is not None:
            result.since = since

        if delta is not None and delta.derived and parent_keys is None:
            for parent_name in delta.via:
                parent = spec_for(parent_name)
                window = None
                if has_window(parent):
                    # A set with only parents takes --since for all of them;
                    # one with a window of its own keeps --since for that.
                    window = (since if not delta.direct else None) or get_watermark(
                        parent.name, parent.delta.field or ""  # type: ignore[union-attr]
                    )
                result.parent_windows[parent.name] = window
            missing = [name for name, window in result.parent_windows.items() if window is None]
            if missing:
                # A parent with no mark (or no date at all) has no window, and
                # reading it whole to collect keys would be the whole child set
                # fifty keys at a time, slower than the full pull it pretends
                # to improve on.
                go_full(f"parent {', '.join(missing)} has no window to read keys by")
            else:
                keys: dict[str, None] = {}
                for parent_name, window in result.parent_windows.items():
                    for key in collect_parent_keys(client, parent_name, delta.via_key or "", window):
                        keys.setdefault(key, None)
                parent_keys = list(keys)

        reads: list[_Read] = []
        if delta is None:
            reads.append(_read_whole(client, spec))
        else:
            if delta.direct:
                reads.append(_read_direct(client, spec, result.since or ""))
            if delta.derived:
                result.parent_keys = len(parent_keys or [])
                if parent_keys:
                    reads.append(_read_keys(client, spec, parent_keys))
                else:
                    logger.info(
                        "%s: %s handed over no changed %s, so there is nothing "
                        "to re-read through them.",
                        spec.name, ", ".join(delta.via), delta.via_key,
                    )
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.seconds = time.monotonic() - started
        logger.error("%s: fetch failed -- %s", spec.name, result.error)
        return result

    rows = _union(spec, *reads) if len(reads) > 1 else (reads[0].rows if reads else [])
    result.rows = len(rows)
    result.requests = sum(r.requests for r in reads)
    expected = [r.expected for r in reads]
    result.counted = None if any(e is None for e in expected) else sum(e or 0 for e in expected)
    result.order_by = next((r.order_by for r in reads if r.order_by), ())
    result.order_by_degraded = any(r.order_by_degraded for r in reads)
    result.unknown_properties = sorted({p for r in reads for p in r.unknown_properties})
    result.columns = columns_of(spec, rows)

    # Duplicates are counted within each read, never across the union: the
    # parts of a delta overlap by design (see _union), while two copies of one
    # row inside a single read are direct evidence of rows lost in paging.
    #
    # identity_keys, not keys: CDPOS's declared key repeats once per field
    # changed in a document, so counting against it would condemn every correct
    # pull of that set as corrupt.
    result.duplicate_keys = sum(
        r.duplicate_keys if r.requests > 1 else count_duplicate_keys(r.rows, spec.identity_keys)
        for r in reads
    )
    result.stable = result.duplicate_keys == 0

    # A read that returns FEWER rows than SAP's own count for the same filter
    # lost rows, and until 2026-10-07 nothing caught it: the check compared
    # against a flag that only said whether $count had answered, so it fired
    # on an empty file and on nothing else, and deltas were exempt outright.
    #
    # POHistorySet was the live example -- 0 rows against a $count of 3,881 on
    # every sweep in late September. That would have landed an empty file,
    # passed the duplicate check perfectly, and told I13 that EKBE has no
    # goods receipts.
    for read in reads:
        shortfall = read.shortfall()
        if shortfall:
            result.stable = False
            result.error = shortfall
            break

    # Where the next increment would start, for a set with a date of its own.
    # Computed before the manifest is written so the file carries it; written
    # to the watermark table further down, and only past a successful landing.
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
            "%s: %d row(s), %d duplicate key(s)%s -- the pull lost rows. "
            "Landed at %s and marked unstable; it must not be loaded.",
            spec.name, result.rows, result.duplicate_keys,
            f"; {result.error}" if result.error else "", prefix,
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
    delta = spec.delta
    payload = {
        "entity_set": spec.name,
        "service": spec.service,
        "target_table": spec.raw_table,
        "keys": list(spec.keys),
        # Provenance. These two tables hold MATERIAL-class change documents
        # rather than every change document in the client, and a reader who
        # does not know that will draw the wrong conclusion from a count.
        "required_filter": spec.required_filter,
        # What an increment was read by, so a disputed row can be traced to
        # the window that brought it in.
        "delta": (
            {
                "field": delta.field,
                "literal": delta.literal,
                "via": list(delta.via),
                "via_key": delta.via_key,
            }
            if delta is not None and result.is_delta
            else None
        ),
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

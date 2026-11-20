"""The OData refresh: sets re-read on a schedule and merged by key.

WHY

The hourly delta reads each set by one date (``manifest.DELTAS``), and for
some sets that date is a CREATION date: a requisition's Badat, a
reservation's Crdat, a material's Ersda. A later change to such a row -- the
requisition released, deleted or turned into a purchase order; the
reservation's quantity edited or the item deleted without a goods issue; the
material flagged for deletion -- leaves the date where it was, and the
hourly window never sees the row again. Measured on PRD on 20 Nov 2026: 32
of the 51 requisitions converted since the 4 Nov load still showed no
purchase order. The CSV full refresh would repair that, but it replaces each
table whole, the columns only the delta carries included (EKPO.AEDAT,
RESB.CRDAT), so it stays off where the delta has been adding them.

WHAT

A list of sets (``ODATA_REFRESH_SETS``), each read whole or by a window over
its own delta date -- "the last 12 months" -- and merged into ``raw_<table>``
by key, the merge the delta uses (``raw_merge``): a matched row is written
only where a value differs, a new row is inserted, nothing is deleted or
replaced. No watermark moves; the delta's increments are untouched. Once a
day at ``ODATA_REFRESH_HOUR_UTC``, under the delta's lock, because the two
merge through the same staging table (``scheduler._odata_refresh_tick``).

A window on a creation date catches every row CREATED in the window,
whatever changed on it since -- which is the point: a requisition raised in
the last year is re-read every night, so its release, its deletion flag and
its purchase order arrive the next morning. Rows older than the window are
left alone, except the ones still OPEN in our table (``OPEN_ROWS``): those
are re-read by key, because an old open requisition or reservation is
exactly the row a late deletion lands on. On PRD, 19% of requisition
deletions come more than a year after the requisition, and 3,015 open
reservations are older than a year.

The window is a setting, so it widens without a deploy. The default (12
months) rests on the same measurement: 99.9% of requisitions convert within
12 months, and 0.45% of goods issues in the last 90 days hit a reservation
older than that.

By hand::

    python -m app.ingest --odata-refresh                       # the configured list
    python -m app.ingest --odata-refresh --set MaterialSet     # one set, whole
    python -m app.ingest --odata-refresh --set PurchaseRequisitionSet --window 24m
"""

from __future__ import annotations

import calendar
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone

from app.core.config import get_settings
from app.core.logging import get_logger
from app.ingest.manifest import IngestSpec, spec_for, verified_deltas

logger = get_logger(__name__)

# --- The window ---------------------------------------------------------------

UNITS = {"d": "day", "w": "week", "m": "month", "y": "year"}

_WINDOW = re.compile(r"^\s*(\d+)\s*([dwmy])\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Window:
    """How far back a set's own window reaches: ``12m``, ``90d``, ``2y``."""

    amount: int
    unit: str

    def start(self, today: date) -> date:
        """The first day the window covers, ``amount`` units before ``today``.

        Months and years step the calendar, with the day clamped to the
        month's length (31 March less one month is 28 or 29 February), so a
        "12 months" window is the same calendar day a year ago.
        """
        if self.unit == "d":
            return date.fromordinal(today.toordinal() - self.amount)
        if self.unit == "w":
            return date.fromordinal(today.toordinal() - 7 * self.amount)
        months = self.amount * (12 if self.unit == "y" else 1)
        total = today.year * 12 + (today.month - 1) - months
        year, month = divmod(total, 12)
        month += 1
        return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))

    def __str__(self) -> str:
        return f"{self.amount}{self.unit}"


def parse_window(text: str) -> Window:
    """``12m`` -> Window(12, "m"). Raises ValueError with the reason."""
    found = _WINDOW.match(text or "")
    if not found:
        raise ValueError(
            f"window {text!r} is not a number followed by d, w, m or y (days, weeks, "
            "months, years), e.g. 12m"
        )
    amount, unit = int(found.group(1)), found.group(2).lower()
    if amount < 1:
        raise ValueError(f"window {text!r} must be at least 1 {UNITS[unit]}")
    return Window(amount, unit)


# --- The list ---------------------------------------------------------------


@dataclass(frozen=True)
class RefreshEntry:
    """One set to refresh: whole, or by a window over its own delta date.

    An entry the setting spelt wrongly carries ``error`` instead, so a typo
    in one name costs that entry, reported every run, not the whole list.
    """

    name: str
    window: Window | None = None
    error: str | None = None

    @property
    def whole(self) -> bool:
        return self.window is None

    def describe(self) -> str:
        if self.error:
            return f"{self.name} (skipped: {self.error})"
        return f"{self.name} whole" if self.whole else f"{self.name} last {self.window}"


def parse_entry(text: str) -> RefreshEntry:
    """``NAME`` or ``NAME:12m``. Never raises: a bad entry carries its error."""
    raw = (text or "").strip()
    name, _, window_text = raw.partition(":")
    name = name.strip()
    if not name:
        return RefreshEntry(name=raw or "(blank)", error="no set name")
    try:
        spec = spec_for(name)
    except KeyError as exc:
        return RefreshEntry(name=name, error=str(exc).strip('"'))
    if not window_text.strip():
        return RefreshEntry(name=spec.name)
    try:
        window = parse_window(window_text)
    except ValueError as exc:
        return RefreshEntry(name=spec.name, error=str(exc))
    delta = spec.delta
    if delta is None or not delta.direct:
        return RefreshEntry(
            name=spec.name,
            error=f"has no date of its own to window on; list it as {spec.name} to read it whole",
        )
    return RefreshEntry(name=spec.name, window=window)


def parse_sets(text: str) -> list[RefreshEntry]:
    """The ``ODATA_REFRESH_SETS`` setting, one entry per comma-separated item."""
    return [parse_entry(item) for item in (text or "").split(",") if item.strip()]


# --- Rows older than the window that are still open ------------------------


@dataclass(frozen=True)
class OpenRows:
    """What "still open" means for a set, over ``raw_<table>`` in the CSV
    route's vocabulary (SAP field names).

    ``key`` is the OData property the rows are re-read by -- the set's first
    key, which SAP filters on (``Banfn eq ...``, ``Rsnum eq ...``: the latter
    is the delta's own hand-over key, the former measured HONOURED in
    filter_support.csv). ``date_column`` is the raw column behind the set's
    delta date, so "older than the window" can be judged in the table; a row
    with no date at all counts as older.
    """

    key: str
    predicate: str
    date_column: str


OPEN_ROWS: dict[str, OpenRows] = {
    # Not deleted, not closed, no purchase order on the requisition row.
    "PurchaseRequisitionSet": OpenRows(
        key="Banfn",
        predicate=(
            "ISNULL(LTRIM(LOEKZ), '') = '' AND ISNULL(LTRIM(EBAKZ), '') = '' "
            "AND ISNULL(LTRIM(EBELN), '') IN ('', '0000000000')"
        ),
        date_column="BADAT",
    ),
    # Not deleted, not final-issued.
    "ReservationItemSet": OpenRows(
        key="Rsnum",
        predicate="ISNULL(LTRIM(XLOEK), '') = '' AND ISNULL(LTRIM(KZEAR), '') = ''",
        date_column="CRDAT",
    ),
}

# More keys than this is not an "older open rows" tail, it is most of the
# table: widen the window instead. 25,000 keys is 500 requests.
OPEN_ROWS_KEY_LIMIT = 25_000


def _as_date(column: str) -> str:
    """A raw date column, whatever shape it holds (DD.MM.YYYY from the CSV,
    YYYYMMDD or ISO from a merge), as a SQL Server date or NULL."""
    return (
        f"COALESCE(TRY_CONVERT(date, [{column}], 104), TRY_CONVERT(date, [{column}], 112), "
        f"TRY_CONVERT(date, [{column}], 23))"
    )


def open_rows_sql(spec: IngestSpec, rule: OpenRows, table: str) -> str:
    """The distinct keys of rows still open and dated before the window."""
    from app.ingest.raw_merge import sap_field
    from app.seed.sqlserver import quote

    key = sap_field(rule.key)
    day = _as_date(rule.date_column)
    return (
        f"SELECT DISTINCT [{key}] FROM {quote(table)} "
        f"WHERE ({rule.predicate}) AND ({day} IS NULL OR {day} < :start) "
        f"AND ISNULL(LTRIM([{key}]), '') <> ''"
    )


def pad_key(value: str, spec: IngestSpec, key_property: str) -> str:
    """A key as OData spells it: SAP's numeric keys travel zero-padded to the
    property's declared length (``Rsnum`` '0002061710'), and a table loaded
    by hand may hold them unpadded."""
    text = str(value).strip()
    found = spec.entity_set.find(key_property)
    width = found.max_length if found else None
    if width and text.isdigit() and len(text) < width:
        return text.zfill(width)
    return text


def open_row_keys(spec: IngestSpec, start: date, *, engine=None) -> list[str]:
    """The keys of this set's rows that are open in our table and older than
    ``start``, or [] for a set with no rule. Raises on a database error."""
    from sqlalchemy import text

    from app.core.db import get_engine
    from app.ingest.raw_merge import raw_table_for

    rule = OPEN_ROWS.get(spec.name)
    target = raw_table_for(spec)
    if rule is None or target is None:
        return []
    engine = engine or get_engine()
    with engine.connect() as connection:
        rows = connection.execute(text(open_rows_sql(spec, rule, target.raw_table)), {"start": start}).fetchall()
    # An all-zero key is SAP's "none" ('0000000000': no reservation), not a
    # row to ask for; PRD's raw_resb holds such rows from the CSV extract.
    keys = sorted({
        key for key in (pad_key(r[0], spec, rule.key) for r in rows if r[0] is not None)
        if key.strip("0")
    })
    if len(keys) > OPEN_ROWS_KEY_LIMIT:
        raise RuntimeError(
            f"{len(keys):,} open {rule.key} value(s) older than {start.isoformat()} in "
            f"{target.raw_table}, above the {OPEN_ROWS_KEY_LIMIT:,} this re-read is sized for; "
            "widen the window instead"
        )
    return keys


# --- The cycle --------------------------------------------------------------


@dataclass
class RefreshOutcome:
    """One set's run, for the log, the CLI table and the summary."""

    name: str
    window: str
    rows: int = 0
    open_keys: int = 0
    requests: int = 0
    seconds: float = 0.0
    raw: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _refresh_one(entry: RefreshEntry, root: str, today: date, include_open: bool, outcome: RefreshOutcome) -> None:
    """Fetch and merge one entry. Raises with the reason on any failure."""
    from app.ingest.fetch import MODE_DELTA, MODE_FULL, fetch_set
    from app.ingest.load import STATUS_SUCCEEDED, load_set

    spec = spec_for(entry.name)
    if spec.blocked:
        raise RuntimeError(f"cannot be read: {spec.blocked}")

    if entry.whole:
        fetched = fetch_set(spec, root=root, mode=MODE_FULL, advance_watermark=False)
    else:
        # The same gate as the delta: a window on a date SAP ignores answers
        # HTTP 200 with the whole set, and this would merge it as if it were
        # the last 12 months.
        _, unverified = verified_deltas([spec])
        if unverified:
            raise RuntimeError(
                f"its delta date is not measured HONOURED on this system -- {unverified[spec.name]}; "
                "a window on an unmeasured date could read the whole set. Probe it "
                f"(python -m app.ingest.delta_probe --set {spec.name}) or list it whole"
            )
        assert entry.window is not None
        start = entry.window.start(today)
        outcome.window = f"last {entry.window} (from {start.isoformat()})"
        keys = open_row_keys(spec, start) if include_open else []
        outcome.open_keys = len(keys)
        rule = OPEN_ROWS.get(spec.name)
        fetched = fetch_set(
            spec, root=root, mode=MODE_DELTA, since=start.isoformat(), parent_keys=[],
            extra_keys=(rule.key, keys) if rule and keys else None, advance_watermark=False,
        )
    outcome.rows = fetched.rows
    outcome.requests = fetched.requests
    if not fetched.ok:
        raise RuntimeError(fetched.error or "fetch unstable")

    loaded = load_set(spec, root=root, prefix=fetched.prefix)
    if loaded.status != STATUS_SUCCEEDED:
        raise RuntimeError(loaded.error or loaded.status)
    outcome.raw = loaded.raw or "loaded"


def run_odata_refresh_cycle(
    entries: list[RefreshEntry] | None = None,
    *,
    today: date | None = None,
    open_rows: bool | None = None,
) -> dict:
    """Every entry in ``ODATA_REFRESH_SETS`` (or ``entries``), read and merged.
    Never raises.

    Synchronous and blocking, like the other cycles; the scheduler runs it in
    a worker thread. The same SAP-system check as the delta: a merge from the
    wrong system is worse than no merge. One entry's failure costs that entry.
    """
    from app.ingest import sap_system

    settings = get_settings()
    chosen = list(entries) if entries is not None else parse_sets(settings.odata_refresh_sets)
    include_open = settings.odata_refresh_open_rows if open_rows is None else open_rows
    today = today or datetime.now(timezone.utc).date()
    # Landed apart from the delta's files: the two would otherwise share a
    # dated prefix and overwrite each other's evidence for the day.
    root = f"{settings.ingest_prefix.strip('/')}/refresh"
    summary: dict = {"pulled": 0, "failed": 0, "errors": [], "sets": []}

    try:
        mismatch = sap_system.verdict(sap_system.recorded(), sap_system.configured())
    except Exception as exc:
        mismatch = f"could not check which SAP system the database holds: {exc}"
    if mismatch:
        summary["errors"] = [f"OData refresh refused: {mismatch}"]
        summary["failed"] = len(chosen)
        logger.error("OData refresh refused: %s", mismatch)
        return summary

    for entry in chosen:
        outcome = RefreshOutcome(name=entry.name, window="whole" if entry.whole else f"last {entry.window}")
        started = time.monotonic()
        if entry.error:
            outcome.error = entry.error
        else:
            try:
                _refresh_one(entry, root, today, include_open, outcome)
            except Exception as exc:
                outcome.error = str(exc) or type(exc).__name__
        outcome.seconds = time.monotonic() - started
        summary["sets"].append(asdict(outcome))
        if outcome.ok:
            summary["pulled"] += 1
            logger.info(
                "%s: refresh merged (%s, %d row(s)%s, %.0fs) -- %s",
                outcome.name, outcome.window, outcome.rows,
                f", {outcome.open_keys} open key(s) re-read" if outcome.open_keys else "",
                outcome.seconds, outcome.raw,
            )
        else:
            summary["failed"] += 1
            summary["errors"].append(f"{outcome.name}: {outcome.error}")
            logger.error("%s: refresh failed (%s) -- %s", outcome.name, outcome.window, outcome.error)
    return summary

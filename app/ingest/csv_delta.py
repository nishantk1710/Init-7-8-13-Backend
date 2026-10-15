"""An increment over the CSV route, for the tables the OData delta does not cover.

    python -m app.ingest --csv-delta                 every table in CSV_DELTA_TABLES
    python -m app.ingest --csv-delta --table RESB    one of them
    python -m app.ingest --csv-delta --since 20261001   a window of your choosing

Off unless CSV_DELTA_ENABLED is true; then the delta cycle runs it after the
OData sweep (app/ingest/scheduler.py). The OData sets keep their own route:
a table here must NOT have a runnable OData delta, and one that does is left
out with a line saying so. Two routes merging into one table would not break
anything -- both merge by key -- but they would do the same work twice.

WHAT ONE INCREMENT IS

    FromDate = the table's mark          ToDate = today          IsDelta = ''

The window is what WE asked for, so the mark is taken from the request, not
from the rows: after a successful merge it moves to ToDate less one day, and
the next window starts there. The one-day overlap is absorbed by the merge.
``IsDelta`` stays blank: SAP's own delta flag has not been confirmed to do
anything (csv_pull module docstring), and this must not depend on it. What the
window selects on is the CSV job's business, per table; for a transaction
table it is a document date of some kind, which is what makes "from the mark
to today" an increment. A table with no mark is SKIPPED, never pulled whole:
the whole-table pull is a replace, and a replace is a decision somebody makes
on purpose (python -m app.ingest --csv-pull --csv-load --table X).

APPEND-ONLY, BY KEY

The landed file is staged and merged into ``raw_<table>`` exactly as the OData
delta is (raw_merge): rows the table already holds are UPDATED in place, rows
it does not are INSERTED, nothing is deleted and the table is never replaced.
The key is the OData set's identity key in SAP spelling (``Rsnum``/``Rspos``
-> ``RSNUM``/``RSPOS``); the batch is reduced to one row per key first; a row
with a blank key is skipped and counted. Re-running the same window changes
nothing.

Keys are written the way SAP stores them. The CSV job drops the leading zeros
of NUMC fields (CHANGENR arrived as 8 digits, BUZEI as 3 -- measured
2026-10-13), and the raw tables were padded back that day. A digits-only key
shorter than the contract's declared length is therefore left-padded before
matching, or every re-read row would be a twin of the one it should update.

A windowed file is marked loaded here so the whole-table loader never sees it:
csv_load refuses a request whose window is narrower than the table's standard
one anyway, because loading it as a replace would shrink the table to the
window.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Column, Engine, MetaData, Table, UnicodeText, inspect, text

from app.core.config import Settings, get_settings
from app.core.db import get_engine
from app.core.logging import get_logger
from app.core.storage import Storage, get_storage
from app.ingest import csv_load, csv_pull, sap_system
from app.ingest.csv_tables import DATE_FORMAT, CsvTable, csv_table
from app.ingest.manifest import spec_for
from app.ingest.raw_merge import sap_field
from app.ingest.watermarks import as_day, get_watermark, set_watermark
from app.models.csv_extract import RECONCILE_INCREMENT, STATUS_OPEN, CsvExtractRequest
from app.seed.sqlserver import quote

logger = get_logger(__name__)

# The watermark row's field. Not an OData property: a stored mark on it can
# never be mistaken for a window on a date column, and get_watermark() ignores
# a mark measured on anything else.
MARK_FIELD = "CsvToDate"

# The mark after a merge is ToDate less this many days: the next window
# re-reads that overlap, which the merge absorbs. One day, not three as for
# the seeded OData marks: here the window end is a day we chose, not a day
# inferred from a load.
MARGIN_DAYS = 1

STAGING_SUFFIX = "__csv_stg"

MERGED = "merged"
EMPTY = "empty"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass
class MergeOutcome:
    table: str
    rows: int = 0
    matched: int = 0
    updated: int = 0
    inserted: int = 0
    rows_without_key: int = 0
    keys_padded: int = 0
    columns_added: list[str] | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def changed_anything(self) -> bool:
        return bool(self.updated or self.inserted or self.columns_added)


@dataclass
class CsvDeltaResult:
    sap_table: str
    status: str
    window: tuple[str, str] | None = None
    request_id: str | None = None
    received: int = 0
    merge: MergeOutcome | None = None
    mark: str | None = None
    seconds: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (MERGED, EMPTY, SKIPPED)


# --- Which tables, and from when -----------------------------------------------


def tables(settings: Settings | None = None, names: list[str] | None = None) -> list[CsvTable]:
    """The CSV tables this increment covers, in the configured order.

    A table with a runnable OData delta is left out and logged: that route
    already keeps it current. Unknown names are logged and left out too.
    """
    settings = settings or get_settings()
    wanted = [n.strip().upper() for n in (names or settings.csv_delta_table_list) if n.strip()]
    chosen: list[CsvTable] = []
    for name in wanted:
        try:
            spec = csv_table(name)
        except KeyError:
            logger.warning("%s: not a CSV table; left out of the CSV delta", name)
            continue
        try:
            odata = spec_for(spec.entity_set)
        except KeyError:
            odata = None
        if odata is not None and odata.runnable_delta is not None:
            logger.warning(
                "%s: %s has a runnable OData delta, which keeps %s current; left out of the CSV delta",
                name, spec.entity_set, spec.raw_table,
            )
            continue
        chosen.append(spec)
    return chosen


def mark_of(spec: CsvTable) -> date | None:
    """The table's CSV mark as a day, or None if it has none."""
    return as_day(get_watermark(spec.entity_set, MARK_FIELD))


def window_for(
    spec: CsvTable, *, since: str | None, today: date, until: str | None = None
) -> tuple[str, str] | None:
    """``(FromDate, ToDate)`` as DATS, or None when there is no mark to start
    from or the range is empty. ``until`` bounds the window; a day after today
    is today."""
    start = as_day(since) if since else mark_of(spec)
    if start is None:
        return None
    end = min(as_day(until) or today, today) if until else today
    if start > end:
        return None
    return start.strftime(DATE_FORMAT), end.strftime(DATE_FORMAT)


def next_mark(stored: date | None, window: tuple[str, str]) -> date | None:
    """Where the mark goes after this window merged, or None to leave it.

    The window's end less MARGIN_DAYS -- but only when the window joined on to
    what was already read: it started on or before the stored mark (or there
    was none). A window that started later would leave the days between the
    mark and its start unread, and moving the mark past them would make that
    gap permanent. Never moves the mark backwards.
    """
    start = datetime.strptime(window[0], DATE_FORMAT).date()
    end = datetime.strptime(window[1], DATE_FORMAT).date()
    candidate = end - timedelta(days=MARGIN_DAYS)
    if stored is not None and start > stored:
        return None
    if stored is not None and candidate <= stored:
        return None
    return candidate


# --- The merge ------------------------------------------------------------------


def _key_columns(spec: CsvTable) -> tuple[list[str], dict[str, int]]:
    """The CSV column names of the set's identity key, and the declared
    length of each where the contract states one (for zero-padding)."""
    odata = spec_for(spec.entity_set)
    lengths = {sap_field(p.name): p.max_length for p in odata.entity_set.properties if p.max_length}
    keys = [sap_field(k) for k in odata.identity_keys]
    return keys, {k: lengths[k] for k in keys if k in lengths}


def pad_key(value: str, width: int | None) -> str:
    """``'1'`` -> ``'0001'`` for a NUMC the CSV job stripped. Digits only, and
    only when shorter: anything else is kept as it came."""
    text_value = (value or "").strip()
    if width and text_value.isdigit() and len(text_value) < width:
        return text_value.zfill(width)
    return text_value


def merge_increment(
    spec: CsvTable,
    data_key: str,
    *,
    storage: Storage | None = None,
    engine: Engine | None = None,
) -> MergeOutcome:
    """Upsert a landed CSV window into ``raw_<table>``. Never raises; the
    outcome says. Nothing is written unless the whole merge succeeds."""
    storage = storage or get_storage()
    engine = engine or get_engine()
    table = spec.raw_table
    outcome = MergeOutcome(table)
    staging = f"{table}{STAGING_SUFFIX}"
    try:
        keys, widths = _key_columns(spec)
        if not inspect(engine).has_table(table):
            outcome.error = (
                f"{table} does not exist, so there is nothing to merge an increment into. "
                f"Build it first: python -m app.ingest --csv-pull --csv-load --table {spec.sap_table}"
            )
            return outcome
        present = [c["name"] for c in inspect(engine).get_columns(table)]
        by_lower = {c.lower(): c for c in present}

        with csv_load._open_csv(storage, data_key) as (header, rows):
            if not header:
                outcome.error = f"{data_key} is empty"
                return outcome
            columns = csv_load._safe_columns(header)
            # The file's columns, spelled as the table spells them; a column
            # the table lacks is added (text, nullable), as the OData merge does.
            col_upper = {c.upper(): i for i, c in enumerate(columns)}
            missing_keys = [k for k in keys if k not in col_upper]
            if missing_keys:
                outcome.error = (
                    f"the file has no column for key(s) {', '.join(missing_keys)}; rows cannot be "
                    "matched, and unmatched inserts are how duplicates start"
                )
                return outcome
            targets = [by_lower.get(c.lower(), c) for c in columns]
            added = [t for t in targets if t.lower() not in by_lower]
            key_idx = [col_upper[k] for k in keys]
            key_targets = [targets[i] for i in key_idx]

            # One row per key, last one wins; keys padded; blank keys skipped.
            by_key: dict[tuple, tuple] = {}
            width = len(columns)
            for row in rows:
                if len(row) < width:
                    row = row + [""] * (width - len(row))
                elif len(row) > width:
                    row = row[:width]
                outcome.rows += 1
                padded = list(row)
                for i, k in zip(key_idx, keys):
                    before = padded[i]
                    padded[i] = pad_key(before, widths.get(k))
                    if padded[i] != (before or "").strip():
                        outcome.keys_padded += 1
                key = tuple(padded[i] for i in key_idx)
                if any(k == "" for k in key):
                    outcome.rows_without_key += 1
                    continue
                by_key[key] = tuple(padded)

        if not by_key:
            outcome.columns_added = []
            return outcome

        with engine.begin() as conn:
            if engine.dialect.name == "mssql":
                add_sql = "ALTER TABLE {t} ADD {c} nvarchar(max) NULL"
            else:
                add_sql = "ALTER TABLE {t} ADD COLUMN {c} TEXT"
            for column in added:
                conn.execute(text(add_sql.format(t=quote(table), c=quote(column))))

            stage = Table(staging, MetaData(), *[Column(c, UnicodeText) for c in targets])
            stage.drop(conn, checkfirst=True)
            stage.create(conn)
            conn.execute(stage.insert(), [dict(zip(targets, row)) for row in by_key.values()])

            target = quote(table)
            stage_name = quote(staging)
            match = " AND ".join(f"s.{quote(k)} = t.{quote(k)}" for k in key_targets)
            non_key = [c for c in targets if c not in key_targets]

            # What actually changes, compared NULL-safely, so a re-read overlap
            # that changed nothing does not count as a change (it is the count
            # that tells the snapshots whether to rebuild).
            if non_key:
                s_cols = ", ".join(f"s.{quote(c)}" for c in non_key)
                t_cols = ", ".join(f"t.{quote(c)}" for c in non_key)
                outcome.updated = conn.execute(text(
                    f"SELECT COUNT(*) FROM {target} t WHERE EXISTS ("
                    f"SELECT 1 FROM {stage_name} s WHERE {match} "
                    f"AND EXISTS (SELECT {s_cols} EXCEPT SELECT {t_cols}))"
                )).scalar() or 0
                if engine.dialect.name == "mssql":
                    assignments = ", ".join(f"t.{quote(c)} = s.{quote(c)}" for c in non_key)
                    outcome.matched = conn.execute(text(
                        f"UPDATE t SET {assignments} FROM {target} t INNER JOIN {stage_name} s ON {match}"
                    )).rowcount
                else:
                    on = " AND ".join(f"s.{quote(k)} = {target}.{quote(k)}" for k in key_targets)
                    assignments = ", ".join(
                        f"{quote(c)} = (SELECT s.{quote(c)} FROM {stage_name} s WHERE {on})" for c in non_key
                    )
                    outcome.matched = conn.execute(text(
                        f"UPDATE {target} SET {assignments} WHERE EXISTS (SELECT 1 FROM {stage_name} s WHERE {on})"
                    )).rowcount
            else:
                outcome.matched = conn.execute(text(
                    f"SELECT COUNT(*) FROM {stage_name} s WHERE EXISTS (SELECT 1 FROM {target} t WHERE {match})"
                )).scalar() or 0

            column_list = ", ".join(quote(c) for c in targets)
            outcome.inserted = conn.execute(text(
                f"INSERT INTO {target} ({column_list}) SELECT {column_list} FROM {stage_name} s "
                f"WHERE NOT EXISTS (SELECT 1 FROM {target} t WHERE {match})"
            )).rowcount
            stage.drop(conn)

        outcome.matched = max(outcome.matched, 0)
        outcome.inserted = max(outcome.inserted, 0)
        outcome.columns_added = added
        if added:
            from app.shared import sap_normalise

            sap_normalise.refresh_after_load(spec.table)
        logger.info(
            "%s: %d row(s) changed (%d matched), %d inserted into %s%s%s%s",
            spec.sap_table, outcome.updated, outcome.matched, outcome.inserted, table,
            f"; {outcome.keys_padded} key value(s) padded to SAP width" if outcome.keys_padded else "",
            f"; added {', '.join(added)}" if added else "",
            f"; {outcome.rows_without_key} row(s) without a key skipped" if outcome.rows_without_key else "",
        )
        return outcome
    except Exception as exc:  # noqa: BLE001 -- the outcome carries it; the transaction rolled back
        outcome.error = f"{type(exc).__name__}: {exc}"
        logger.error("%s: merge into %s failed -- %s", spec.sap_table, table, outcome.error)
        try:
            with engine.begin() as conn:
                Table(staging, MetaData()).drop(conn, checkfirst=True)
        except Exception:  # noqa: BLE001
            logger.warning("could not drop %s after a failed merge", staging)
        return outcome


# --- One pass ---------------------------------------------------------------------


def run_csv_delta(
    names: list[str] | None = None,
    *,
    since: str | None = None,
    until: str | None = None,
    today: date | None = None,
    storage: Storage | None = None,
    settings: Settings | None = None,
) -> list[CsvDeltaResult]:
    """Pull each table's window, merge it, advance its mark. One table at a
    time -- the CSV route serialises per table on SAP's side. Never raises."""
    settings = settings or get_settings()
    today = today or datetime.now(timezone.utc).date()
    results: list[CsvDeltaResult] = []

    try:
        sap_system.ensure()
    except Exception as exc:  # noqa: BLE001
        logger.error("CSV delta refused: %s", exc)
        return [CsvDeltaResult("*", FAILED, error=f"refused: {exc}")]

    for spec in tables(settings, names):
        started = time.monotonic()
        result = CsvDeltaResult(spec.sap_table, FAILED)
        results.append(result)
        if spec.blocked:
            result.status, result.error = SKIPPED, spec.blocked
            continue
        stored = mark_of(spec)
        window = window_for(spec, since=since, today=today, until=until)
        if window is None:
            result.status = SKIPPED
            if since is None and stored is None:
                result.error = (
                    f"no CSV mark for {spec.entity_set}; not pulled whole. Seed one at the day the table "
                    f"was last extracted: set_watermark('{spec.entity_set}', '{MARK_FIELD}', 'YYYY-MM-DD', 0)"
                )
            else:
                result.error = f"empty range: starts after it ends ({since or stored}..{until or today})"
            logger.warning("%s: %s", spec.sap_table, result.error)
            continue
        result.window = window
        from_date, to_date = window

        fired = csv_pull.fire(spec.sap_table, from_date=from_date, to_date=to_date)
        result.request_id = fired.request_id or None
        if fired.status != STATUS_OPEN:
            result.error = fired.error or fired.status
            result.seconds = time.monotonic() - started
            continue
        _tag_increment(fired.request_id)
        pulled = csv_pull.wait_for(
            fired.request_id,
            first_chunk_timeout=settings.csv_delta_first_chunk_timeout_seconds,
            quiet_period=settings.csv_delta_quiet_period_seconds,
        )
        result.received = pulled.received_rows
        if pulled.received_rows <= 0:
            # Nothing arrived: a window with no changes looks exactly like a
            # job that never ran. The mark stays, the next cycle re-asks the
            # same window, and the operator sees it in the log either way.
            result.status = EMPTY
            result.error = pulled.error
            result.seconds = time.monotonic() - started
            logger.info("%s: no rows for %s..%s (%s); mark held", spec.sap_table, from_date, to_date,
                        pulled.status)
            continue
        if not pulled.ok or not pulled.data_key:
            result.error = pulled.error or pulled.status
            result.seconds = time.monotonic() - started
            continue

        started_at = datetime.now(timezone.utc)
        merged = merge_increment(spec, pulled.data_key, storage=storage)
        result.merge = merged
        record = _request(fired.request_id)
        if not merged.ok:
            result.error = merged.error
            if record is not None:
                csv_load._record(spec, record, csv_load.STATUS_FAILED, 0, started_at, merged.error)
            result.seconds = time.monotonic() - started
            continue

        # Loaded, from the whole-table loader's point of view: it must never
        # take this window for a replacement.
        csv_load._mark_loaded(fired.request_id)
        if merged.changed_anything and record is not None:
            csv_load._record(spec, record, csv_load.STATUS_SUCCEEDED, merged.rows, started_at, None)
        new_mark = next_mark(stored, window)
        if new_mark is not None:
            set_watermark(spec.entity_set, MARK_FIELD, new_mark.isoformat(), merged.rows)
            result.mark = new_mark.isoformat()
        else:
            logger.info(
                "%s: window %s..%s merged; mark held at %s (the window did not join on to it, "
                "or ends before it)", spec.sap_table, *window, stored,
            )
        result.status = MERGED
        result.seconds = time.monotonic() - started
    return results


def _request(request_id: str) -> CsvExtractRequest | None:
    from app.core.db import get_sessionmaker

    with get_sessionmaker()() as session:
        record = session.get(CsvExtractRequest, request_id)
        if record is not None:
            session.expunge(record)
        return record


def _tag_increment(request_id: str) -> None:
    """Mark the request as a window, so the whole-table loader refuses it
    (csv_load) whatever else happens. The verdict still treats it as a
    bounded delivery: a window is a subset of the $count."""
    from app.core.db import get_sessionmaker

    with get_sessionmaker()() as session:
        record = session.get(CsvExtractRequest, request_id)
        if record is not None:
            record.reconcile = RECONCILE_INCREMENT
            session.commit()


def summary(results: list[CsvDeltaResult]) -> dict[str, Any]:
    """The scheduler's numbers for one pass."""
    return {
        "merged": sum(1 for r in results if r.status == MERGED),
        "empty": sum(1 for r in results if r.status == EMPTY),
        "skipped": sum(1 for r in results if r.status == SKIPPED),
        "failed": sum(1 for r in results if r.status == FAILED),
        "rows": sum(r.merge.rows for r in results if r.merge),
        "inserted": sum(r.merge.inserted for r in results if r.merge),
        "errors": [f"{r.sap_table}: {r.error}" for r in results if r.status == FAILED and r.error],
    }

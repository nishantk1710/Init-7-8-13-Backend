"""Stage two for the CSV route: file a landed extract into Azure SQL.

Sibling of ``ingest/load.py``, and it reuses the same machinery -- the seed's
``RawTableWriter`` creates, fills and indexes the table, ``IngestionRun``
records what happened, and every column is created as text for the reason the
seed spells out: ``000000002000000270`` coerced to a number becomes
``2e+15`` irreversibly. Typing belongs in the normalise step.

TWO NAMESPACES, DELIBERATELY

This writes ``raw_<table>``; ``ingest/load.py`` writes ``odata_<table>``. The
CSV extract of EKPO carries 277 columns and the OData set carries 19, so one
shared table would mean the narrow delta nulling 258 columns on every row it
touched -- a merge reporting success while destroying data. Separate tables
keep both honest, and the serving layer joins what it needs.

THE GATE

A request that did not reconcile is refused, for the same reason load.py
refuses an unstable fetch: a short file loads without complaint and leaves a
plausible, wrong dataset in front of people with no way to tell. ``csv_pull``
decides reconciliation; this module only declines to argue with it.

THE WATERMARK

A CSV full pull writes no watermark by itself, so the first OData delta after
one would find nothing to start from and fall back to a full pull. The seed
is taken from the file's own newest date, after the rows are in, so a failed
load never advances it -- and only when the set has no mark at all. A mark the
OData delta measured itself is a position in ``odata_<table>``; this load
filled ``raw_<table>``, a different table, and overwriting a measured mark
with the CSV's date would skip, for ``odata_<table>``, every change between
the two.
"""

from __future__ import annotations

import contextlib
import csv
import io
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.db import get_engine, get_sessionmaker
from app.core.logging import get_logger
from app.core.storage import Storage, get_storage
from app.ingest.csv_tables import CsvTable, csv_table
from app.ingest.watermarks import get_watermark, set_watermark
from app.models.csv_extract import STATUS_COMPLETE, STATUS_OPEN, CsvExtractRequest
from app.models.ingestion import IngestionRun
from app.seed.sqlserver import RawTableWriter

logger = get_logger(__name__)

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# Rows per executemany batch. Matches the seed; a 277-column table at 50,000
# rows a batch would hold far too much in memory at once.
BATCH_ROWS = 5_000

# A field longer than this is still read. SAP free-text columns run long and
# the csv module raises rather than truncating.
csv.field_size_limit(16 * 1024 * 1024)

# Date columns worth seeding a watermark from, per SAP table. The OData delta
# filters on the first of these it finds, so the seed has to be the same field
# or the increment starts from the wrong place.
WATERMARK_FIELD: dict[str, tuple[str, str]] = {
    # sap_table -> (CSV column, OData property the delta filters on)
    "EKKO": ("AEDAT", "Aedat"),
    "MKPF": ("BUDAT", "Budat"),
    "CDHDR": ("UDATE", "Udate"),
}


@dataclass
class CsvLoadResult:
    sap_table: str
    table: str
    status: str
    rows: int = 0
    columns: int = 0
    seconds: float = 0.0
    watermark: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCEEDED


def latest_complete(sap_table: str) -> CsvExtractRequest | None:
    """The newest reconciled, not-yet-loaded request for this table."""
    sessionmaker = get_sessionmaker()
    with sessionmaker() as session:
        return session.scalars(
            select(CsvExtractRequest)
            .where(
                CsvExtractRequest.sap_table == sap_table.upper(),
                CsvExtractRequest.status == STATUS_COMPLETE,
                CsvExtractRequest.loaded_at.is_(None),
            )
            .order_by(CsvExtractRequest.completed_at.desc())
        ).first()


def latest_landed(sap_table: str) -> CsvExtractRequest | None:
    """The newest not-yet-loaded request that received anything, whatever
    its verdict. For ``--allow-unstable`` only: a file two rows short of its
    $count is still a file, and loading it on purpose is a decision an
    operator is allowed to make, once, with the shortfall on record.
    """
    sessionmaker = get_sessionmaker()
    with sessionmaker() as session:
        return session.scalars(
            select(CsvExtractRequest)
            .where(
                CsvExtractRequest.sap_table == sap_table.upper(),
                CsvExtractRequest.status != STATUS_OPEN,
                CsvExtractRequest.received_rows > 0,
                CsvExtractRequest.loaded_at.is_(None),
            )
            .order_by(CsvExtractRequest.fired_at.desc())
        ).first()


def _clean_lines(handle) -> Iterator[str]:
    """Decoded lines with NUL bytes removed, one at a time.

    A UTF-16 export mislabelled as 8-bit decodes to text peppered with NULs and
    the csv module raises on them rather than skipping. Stripping per line
    keeps that protection without holding the file in memory to do it.
    """
    for line in handle:
        yield line.replace("\x00", "") if "\x00" in line else line


@contextlib.contextmanager
def _open_csv(storage: Storage, key: str) -> Iterator[tuple[list[str], Iterator[list[str]]]]:
    """Header and a lazy row iterator, valid inside the ``with`` block only.

    Streamed, not materialised: ``open_read`` spools the object to a temporary
    file and a TextIOWrapper over it feeds csv.reader one line at a time, so
    a gigabyte extract costs a buffer, not a gigabyte.

    A context manager, and it has to be. The adapter's ``open_read`` is a
    generator-based context manager whose ``finally`` closes the spool. An
    earlier version called ``.__enter__()`` on it and returned the handle;
    with nothing left referencing the manager, CPython finalised the
    generator on the spot, the ``finally`` ran, and the very first row read
    "I/O operation on closed file" -- on every one of 18 tables, on the real
    adapter only, because the test fake did not close on finalisation. The
    manager lives for exactly as long as this block does, so the rows do too.
    """
    with storage.open_read(key) as handle:
        text_handle = io.TextIOWrapper(
            handle, encoding="utf-8-sig", errors="replace", newline=""
        )
        reader = csv.reader(_clean_lines(text_handle))
        header = next(reader, None)
        yield ([c.strip() for c in header] if header else []), reader


def _safe_columns(header: list[str]) -> list[str]:
    """Usable column names, with SAP's blank and duplicate headers resolved.

    The EKPO extract really does carry unnamed columns -- a run of them sits
    between INCO3_L and ZZKTWRT. Left as-is they all map to the same empty key
    and the last one silently wins, so each gets a positional name instead.
    """
    seen: dict[str, int] = {}
    columns: list[str] = []
    for index, raw in enumerate(header):
        name = (raw or "").strip() or f"column_{index + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        columns.append(name)
    return columns


def load_table(
    sap_table: str,
    *,
    request: CsvExtractRequest | None = None,
    storage: Storage | None = None,
    allow_unreconciled: bool = False,
) -> CsvLoadResult:
    """Load one landed CSV extract into ``raw_<table>``. Never raises."""
    spec = csv_table(sap_table)
    storage = storage or get_storage()
    started = time.monotonic()
    started_at = datetime.now(timezone.utc)

    def failure(detail: str) -> CsvLoadResult:
        logger.error("%s: %s", spec.sap_table, detail)
        return CsvLoadResult(
            spec.sap_table, spec.raw_table, STATUS_FAILED,
            seconds=time.monotonic() - started, error=detail,
        )

    record = request or (
        latest_landed(spec.sap_table)
        if allow_unreconciled
        else latest_complete(spec.sap_table)
    )
    if record is None:
        return CsvLoadResult(
            spec.sap_table, spec.raw_table, STATUS_SKIPPED,
            seconds=time.monotonic() - started,
            error=(
                f"no reconciled, unloaded extract for {spec.sap_table}. Pull one "
                f"first: python -m app.ingest --csv-pull --table {spec.sap_table}"
            ),
        )

    if record.status != STATUS_COMPLETE and not allow_unreconciled:
        return failure(
            f"request {record.request_id} is {record.status}, not complete "
            f"({record.error or 'no detail'}). Refusing to load it: a short file "
            "reads as a whole one."
        )
    if record.status != STATUS_COMPLETE:
        logger.warning(
            "%s: loading request %s although it is %s (%s) -- --allow-unstable",
            spec.sap_table, record.request_id, record.status,
            record.error or "no detail",
        )

    if not record.data_key:
        return failure(f"request {record.request_id} completed with no file key")

    writer = RawTableWriter(indexed_columns=())
    loaded = 0
    columns: list[str] = []
    watermark_value: str | None = None
    watermark_index: int | None = None
    phase = "reading"

    try:
        with _open_csv(storage, record.data_key) as (header, rows):
            if not header:
                return failure(f"{record.data_key} is empty")

            columns = _safe_columns(header)
            watermark_index = _watermark_index(spec, columns)
            phase = "loading"

            engine = get_engine()
            with engine.begin() as connection:
                # driver_connection, and a cursor from it: SQLAlchemy's wrapper
                # does more on exit than close, and engine.begin() owns the
                # transaction. Same pattern as ingest/load.py.
                cursor = connection.connection.driver_connection.cursor()

                # Replace, not merge. A full pull IS the table; merging it into
                # the previous one would keep rows SAP has since deleted.
                writer.create_table(cursor, spec.raw_table, columns)
                for batch in _batches(rows, len(columns)):
                    if watermark_index is not None:
                        watermark_value = _highest(
                            watermark_value, (r[watermark_index] for r in batch)
                        )
                    writer.bulk_load(cursor, spec.raw_table, columns, batch)
                    loaded += len(batch)
    except Exception as exc:
        if phase == "reading":
            return failure(f"could not read {record.data_key}: {exc}")
        _record(spec, record, STATUS_FAILED, 0, started_at, str(exc))
        return failure(f"the load failed after {loaded} row(s): {exc}")

    elapsed = time.monotonic() - started

    # Only past a successful load. A watermark advanced by a failed one leaves
    # a gap nothing will ever go back for.
    seeded: str | None = None
    if watermark_value and watermark_index is not None:
        _, odata_field = WATERMARK_FIELD[spec.sap_table]
        try:
            seeded = _seed_watermark(spec.entity_set, odata_field, watermark_value, loaded)
            if seeded:
                logger.info(
                    "%s: watermark seeded at %s.%s = %s (from DATS %s)",
                    spec.sap_table, spec.entity_set, odata_field, seeded, watermark_value,
                )
        except Exception as exc:
            # Loud, but not fatal: the rows are in. The next delta will do a
            # full pull and say so, which is recoverable; failing the load
            # here would throw away work that succeeded.
            logger.error(
                "%s: rows loaded but the watermark could not be seeded (%s). "
                "The next delta will fall back to a full pull.",
                spec.sap_table, exc,
            )

    _mark_loaded(record.request_id)
    _record(spec, record, STATUS_SUCCEEDED, loaded, started_at, None)
    logger.info(
        "%s: %d row(s), %d column(s) into %s in %.1fs",
        spec.sap_table, loaded, len(columns), spec.raw_table, elapsed,
    )
    return CsvLoadResult(
        spec.sap_table, spec.raw_table, STATUS_SUCCEEDED,
        rows=loaded, columns=len(columns), seconds=elapsed, watermark=seeded,
    )


def _seed_watermark(entity_set: str, field: str, dats: str, rows: int) -> str | None:
    """Give the OData delta a starting point, if it has none. Returns the mark.

    One day back, in the OData delta's own shape. SAP serialises a DATS as
    midnight in its own time zone, which the envelope decodes to 22:00 UTC
    the evening before; a literal at midnight of the same date could sit just
    past every row of that day, depending on which zone SAP reads the literal
    in. ``ge`` from the previous day re-reads at most one day of rows, and
    the merge absorbs the overlap.

    Only if the set has no usable mark. See the module docstring: a mark the
    delta measured itself belongs to a different table than this load filled.
    """
    if get_watermark(entity_set, field) is not None:
        logger.info(
            "%s: already has a watermark on %s; the CSV load leaves it alone",
            entity_set, field,
        )
        return None
    try:
        newest = datetime.strptime(dats, "%Y%m%d")
    except ValueError:
        # "00000000" is SAP for "no date"; a column of those seeds nothing.
        return None
    mark = f"{newest - timedelta(days=1):%Y-%m-%d %H:%M:%S}"
    set_watermark(entity_set, field, mark, rows)
    return mark


def _watermark_index(spec: CsvTable, columns: list[str]) -> int | None:
    entry = WATERMARK_FIELD.get(spec.sap_table)
    if entry is None:
        return None
    csv_column = entry[0]
    upper = [c.upper() for c in columns]
    return upper.index(csv_column) if csv_column in upper else None


def _highest(current: str | None, values: Iterator[str]) -> str | None:
    """Newest DATS string seen. Lexical order is chronological for yyyymmdd."""
    for value in values:
        candidate = (value or "").strip()
        if len(candidate) == 8 and candidate.isdigit():
            if current is None or candidate > current:
                current = candidate
    return current


def _batches(rows: Iterator[list[str]], width: int) -> Iterator[list[tuple]]:
    """Fixed-width tuples, in batches.

    Ragged rows are padded or trimmed rather than refused. SAP exports do carry
    them, and losing an extract to one malformed line near the end would be the
    same packaging-over-contents mistake the receiver exists to avoid.
    """
    batch: list[tuple] = []
    for row in rows:
        if len(row) < width:
            row = row + [""] * (width - len(row))
        elif len(row) > width:
            row = row[:width]
        batch.append(tuple(row))
        if len(batch) >= BATCH_ROWS:
            yield batch
            batch = []
    if batch:
        yield batch


def _mark_loaded(request_id: str) -> None:
    sessionmaker = get_sessionmaker()
    with sessionmaker() as session:
        record = session.get(CsvExtractRequest, request_id)
        if record is not None:
            record.loaded_at = datetime.now(timezone.utc)
            session.commit()


def _record(
    spec: CsvTable,
    request: CsvExtractRequest,
    status: str,
    rows: int,
    started_at: datetime,
    error: str | None,
) -> None:
    """Leave the same audit trail the seed and the OData loader leave."""
    try:
        sessionmaker = get_sessionmaker()
        with sessionmaker() as session:
            session.add(
                IngestionRun(
                    source_file=f"csv:{spec.sap_table}:{request.request_id}",
                    target_table=spec.raw_table,
                    row_count=rows,
                    status=status,
                    error=error[:4000] if error else None,
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc),
                )
            )
            session.commit()
    except Exception:
        logger.exception("could not record the ingestion run for %s", spec.sap_table)


def load_all(
    tables: list[str] | None = None, *, allow_unreconciled: bool = False
) -> list[CsvLoadResult]:
    """Every table with a reconciled extract waiting. Independent, so one
    failure does not stop the rest."""
    from app.ingest.csv_tables import CSV_TABLES

    names = tables or [t.sap_table for t in CSV_TABLES]
    results = []
    for name in names:
        result = load_table(name, allow_unreconciled=allow_unreconciled)
        if result.status != STATUS_SKIPPED:
            results.append(result)
    return results

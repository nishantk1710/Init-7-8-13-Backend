"""Load extract workbooks into the raw layer.

The path is XLSX -> generator -> ``fast_executemany`` -> Azure SQL, with no list,
no temporary CSV and no ORM object anywhere in between. That matters twice: the
bulk path is orders of magnitude faster than row-by-row inserts across 3.3
million rows, and streaming keeps memory flat regardless of file size.

The SQL Server specifics -- bracket quoting, bounded widths on indexed columns,
the extended property that carries the table's warning -- live in
``app/seed/sqlserver.py``. This module orchestrates; that one writes.

Every column is created as ``text``. The extract carries SAP keys as digit
strings -- material numbers, plants, document numbers -- and any numeric
coercion at load time turns ``000000008000000000`` into ``8e+15`` irreversibly.
Typing belongs in the normalise step, against a reviewed mapping.

Each table is loaded in one transaction: drop, create, copy every file, record
the audit rows, commit. A failure rolls that table back to its previous contents
and moves on to the next -- one malformed workbook must not cost the other 27.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_engine, get_sessionmaker
from app.core.logging import get_logger
from app.core.storage import Storage, get_storage, sha256_of
from app.models.ingestion import IngestionRun
from app.seed.manifest import EXTRACTS, ExtractSpec
from app.seed.reader import read_headers, read_rows
from app.seed.sqlserver import RawTableWriter

logger = get_logger(__name__)

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


@dataclass
class TableResult:
    """Outcome of loading one table."""

    table: str
    status: str
    rows: int = 0
    seconds: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_SUCCEEDED, STATUS_SKIPPED)


def _fingerprints(storage: Storage, spec: ExtractSpec) -> dict[str, str]:
    """SHA-256 per source file, so a re-run can tell new content from a re-run."""
    return {key: sha256_of(storage, key) for key in spec.files}


def _already_loaded(session: Session, spec: ExtractSpec, fingerprints: dict[str, str]) -> bool:
    """Whether every file of this table is already loaded at exactly this content.

    Checked per table rather than per file: the table is truncated once and all
    its files appended, so a partial skip would silently produce a partial table.
    """
    recorded = session.execute(
        select(IngestionRun.source_file, IngestionRun.source_sha256).where(
            IngestionRun.target_table == spec.raw_table,
            IngestionRun.status == STATUS_SUCCEEDED,
        )
    ).all()
    latest = {source_file: digest for source_file, digest in recorded}
    return all(latest.get(key) == digest for key, digest in fingerprints.items())


def _table_description(spec: ExtractSpec) -> str:
    """The warning attached to the table itself.

    The raw layer mirrors the EXTRACT, not the OData contract: business-label
    column names, unpadded material numbers, everything text. Code written
    against it will break when the same data arrives from CPI.

    That warning lives in the README and the manifest, but neither is visible
    to someone who found the table in a database client and started writing
    SQL. This puts it where they will actually see it -- "backslash-d-plus" in psql, the
    table description in DBeaver, pgAdmin or SSMS.
    """
    warning = (
        f"RAW extract layer, from {', '.join(spec.files)}. "
        "Column names are the EXTRACT's business labels, not SAP field names or "
        "OData properties; material numbers are unpadded; every column is text. "
        "Do NOT build on this directly -- it does not match what CPI returns, so "
        "anything written against it needs rewriting at cutover. Read the "
        "normalised layer instead once it exists. See README, 'Seeding'."
    )
    if spec.note:
        warning += f" NOTE: {spec.note}"
    return warning


# Columns worth an index on every table that has one. These are what the three
# initiatives join and filter on, and without them a join across two raw tables
# is two sequential scans over millions of rows -- measured here at minutes for a
# query that should take under a second.
INDEXED_COLUMNS = (
    "material",
    "plant",
    "mat_code",
    "purchasing_document",
    "purchase_requisition",
    "material_document",
    "reservation_number",
    "supplier",
    "object_value",
    "document_number",
)


def _describe_table(writer: RawTableWriter, spec: ExtractSpec) -> None:
    """Attach the table's warning, in a transaction of its own. Never raises."""
    try:
        with get_engine().begin() as connection:
            cursor = connection.connection.driver_connection.cursor()
            try:
                writer.describe_table(cursor, spec.raw_table, _table_description(spec))
            finally:
                cursor.close()
    except Exception as exc:
        logger.warning("%s: could not describe the table: %s", spec.table, exc)


def _create_indexes(
    writer: RawTableWriter, cursor: object, table: str, columns: list[str]
) -> list[str]:
    """Index the join columns this table actually has. Returns the ones created."""
    created = []
    for column in INDEXED_COLUMNS:
        if column not in columns:
            continue
        writer.create_index(cursor, table, column)
        created.append(column)
    return created


def _load_file(
    writer: RawTableWriter,
    cursor: object,
    storage: Storage,
    table: str,
    key: str,
    columns: list[str],
    spec: ExtractSpec,
) -> int:
    """Stream one workbook into ``table``. Returns the row count.

    The generator is handed to the writer rather than consumed here, so the rows
    never exist as a list on either backend.
    """
    return writer.bulk_load(
        cursor,
        table,
        columns,
        read_rows(
            storage, key, expected=columns, sheet=spec.sheet, header_row=spec.header_row
        ),
    )


def load_table(spec: ExtractSpec, *, force: bool = False) -> TableResult:
    """Load one table from its workbook(s). Never raises -- failures are returned."""
    storage = get_storage()
    started = time.monotonic()
    started_at = datetime.now(timezone.utc)

    try:
        fingerprints = _fingerprints(storage, spec)
    except Exception as exc:
        logger.error("%s: could not read source files: %s", spec.table, exc)
        return TableResult(spec.table, STATUS_FAILED, error=f"{type(exc).__name__}: {exc}")

    session_factory = get_sessionmaker()

    if not force:
        with session_factory() as session:
            if _already_loaded(session, spec, fingerprints):
                logger.info("%s: unchanged since last load, skipping", spec.table)
                return TableResult(spec.table, STATUS_SKIPPED)

    try:
        columns = read_headers(
            storage, spec.files[0], sheet=spec.sheet, header_row=spec.header_row
        )

        writer = RawTableWriter(INDEXED_COLUMNS)

        per_file: dict[str, int] = {}
        with get_engine().begin() as connection:
            raw_connection = connection.connection.driver_connection
            # try/finally rather than `with`: pyodbc's cursor context manager
            # does more on exit than close, and closing is the only behaviour
            # wanted here. The surrounding engine.begin() owns the transaction.
            cursor = raw_connection.cursor()
            try:
                writer.create_table(cursor, spec.raw_table, columns)
                for key in spec.files:
                    logger.info("%s: loading %s", spec.table, key)
                    per_file[key] = _load_file(
                        writer, cursor, storage, spec.raw_table, key, columns, spec
                    )
                # After the rows, not before: maintaining an index during a bulk
                # load costs more than building it once at the end.
                indexed = _create_indexes(writer, cursor, spec.raw_table, columns)
                if indexed:
                    logger.info("%s: indexed %s", spec.table, ", ".join(indexed))
            finally:
                cursor.close()

        # Deliberately AFTER the load has committed, in its own transaction.
        # The description is a warning for whoever finds this table in SSMS --
        # worth having, worth nothing next to the rows. Inside the load's
        # transaction a failing sp_addextendedproperty could doom it and take
        # the whole table down at commit, which is a spectacular way to lose
        # 3.3 million rows to a comment.
        _describe_table(writer, spec)

        total = sum(per_file.values())
        elapsed = time.monotonic() - started
        finished_at = datetime.now(timezone.utc)

        with session_factory() as session:
            # Supersede earlier audit rows for these files, so the table holds
            # the current state rather than an ever-growing history of reloads.
            session.query(IngestionRun).filter(
                IngestionRun.target_table == spec.raw_table
            ).delete(synchronize_session=False)
            for key, rows in per_file.items():
                session.add(
                    IngestionRun(
                        source_file=key,
                        target_table=spec.raw_table,
                        row_count=rows,
                        source_sha256=fingerprints[key],
                        status=STATUS_SUCCEEDED,
                        started_at=started_at,
                        finished_at=finished_at,
                    )
                )
            session.commit()

        rate = int(total / elapsed) if elapsed > 0 else 0
        logger.info(
            "%s: %d rows into %s in %.1fs (%d rows/s)",
            spec.table,
            total,
            spec.raw_table,
            elapsed,
            rate,
        )
        return TableResult(spec.table, STATUS_SUCCEEDED, rows=total, seconds=elapsed)

    except Exception as exc:
        elapsed = time.monotonic() - started
        detail = f"{type(exc).__name__}: {exc}"
        logger.error("%s: load failed after %.1fs -- %s", spec.table, elapsed, detail)

        # Record the failure so `--all` leaves an auditable trail rather than a
        # silent gap. Best effort: if the database is what failed, this will too.
        try:
            with session_factory() as session:
                session.add(
                    IngestionRun(
                        source_file=spec.files[0],
                        target_table=spec.raw_table,
                        row_count=0,
                        status=STATUS_FAILED,
                        error=detail[:4000],
                        started_at=started_at,
                        finished_at=datetime.now(timezone.utc),
                    )
                )
                session.commit()
        except Exception:
            logger.exception("%s: could not record the failure either", spec.table)

        return TableResult(spec.table, STATUS_FAILED, seconds=elapsed, error=detail)


def missing_files(specs: tuple[ExtractSpec, ...] = EXTRACTS) -> dict[str, list[str]]:
    """Manifest keys with no object in storage, keyed by table.

    Run before any table is touched. Without it, a wrong STORAGE_URL or a
    differently-named delivery is discovered one table at a time, after several
    have already been dropped and rebuilt.
    """
    available = set(get_storage().list())
    gaps = {}
    for spec in specs:
        absent = [key for key in spec.files if key not in available]
        if absent:
            gaps[spec.table] = absent
    return gaps


def load_all(
    specs: tuple[ExtractSpec, ...] = EXTRACTS, *, force: bool = False
) -> list[TableResult]:
    """Load every table, continuing past failures."""
    results = []
    for index, spec in enumerate(specs, start=1):
        logger.info("[%d/%d] %s", index, len(specs), spec.table)
        results.append(load_table(spec, force=force))
    return results

"""Stage two: load a landed JSONL file into the raw layer.

This is the seed loader's sibling and reuses its machinery unchanged --
``RawTableWriter`` creates, fills and indexes the table, and ``IngestionRun``
records what happened. The only difference is where the rows come from.

Every column is created as text, for the same reason the seed does it: SAP keys
are digit strings, and ``000000002000000270`` coerced to a number becomes
``2e+15`` irreversibly. Typing belongs in the normalise step, against a
reviewed mapping.

One rule this loader adds: **a fetch marked unstable is refused.** The client
measures duplicate keys against ``$inlinecount`` on every pull, and a file that
failed that check is a file with rows missing. Loading it would put a plausible
but wrong dataset in front of people who have no way to tell.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.db import get_engine, get_sessionmaker
from app.core.logging import get_logger
from app.core.storage import Storage, get_storage
from app.ingest.fetch import DATA_FILE, MANIFEST_FILE, read_manifest
from app.ingest.manifest import IngestSpec
from app.models.ingestion import IngestionRun
from app.seed.sqlserver import RawTableWriter, quote

logger = get_logger(__name__)

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# Suffix for the scratch table a merge stages rows through. Double underscore
# so it cannot collide with an entity set whose name happens to end in "stg".
STAGING_SUFFIX = "__stg"


def _table_exists(cursor, table: str) -> bool:
    cursor.execute("SELECT OBJECT_ID(?, 'U')", f"dbo.{table}")
    return cursor.fetchone()[0] is not None


def table_exists(table: str) -> bool:
    """Whether ``dbo.<table>`` exists, on a connection of its own.

    Asked by the sweep before a delta is fetched: an increment with no table
    to merge into is a full pull, not an error found at load time.
    """
    with get_engine().connect() as connection:
        found = connection.execute(
            text("SELECT OBJECT_ID(:t, 'U')"), {"t": f"dbo.{table}"}
        ).scalar()
    return found is not None


def _existing_columns(cursor, table: str) -> list[str]:
    cursor.execute(
        "SELECT name FROM sys.columns WHERE object_id = OBJECT_ID(?, 'U') "
        "ORDER BY column_id",
        f"dbo.{table}",
    )
    return [row[0] for row in cursor.fetchall()]


def _merge(
    cursor,
    writer: RawTableWriter,
    spec: IngestSpec,
    columns: list[str],
    rows_iter,
) -> int:
    """Upsert a delta batch into the existing table, keyed on the entity key.

    Staged rather than merged row by row: the incoming batch goes into a
    scratch table through the same bulk path a full load uses, the matching
    rows are deleted from the target in one statement, and the batch is
    inserted in another. Three set-based operations instead of N round trips.

    Delete-then-insert rather than SQL Server's MERGE. MERGE would do this in
    one statement and has a long history of surprising behaviour under
    concurrency; these two statements are boring, and they run inside the
    caller's transaction so the pair is still atomic.
    """
    target = spec.raw_table
    staging = f"{target}{STAGING_SUFFIX}"

    present = _existing_columns(cursor, target)
    if set(present) != set(columns):
        added = sorted(set(columns) - set(present))
        dropped = sorted(set(present) - set(columns))
        raise RuntimeError(
            f"{target} has a different shape from this delta "
            f"(new: {added or 'none'}; missing: {dropped or 'none'}). "
            "Merging would either drop a column SAP has started sending or "
            "leave one half-populated. Run a full pull for this set instead: "
            f"python -m app.ingest --fetch --load --full --set {spec.name}"
        )

    writer.create_table(cursor, staging, columns)
    try:
        staged = writer.bulk_load(cursor, staging, columns, rows_iter)

        # identity_keys: the merge must delete exactly the rows this batch
        # replaces. Joining on a key that is not unique would delete every row
        # sharing it -- for CDPOS, every field change in the same document,
        # when the batch only carries one of them.
        on_clause = " AND ".join(
            f"t.{quote(key)} = s.{quote(key)}" for key in spec.identity_keys
        )
        cursor.execute(
            f"DELETE t FROM {quote(target)} t "
            f"INNER JOIN {quote(staging)} s ON {on_clause}"
        )
        replaced = cursor.rowcount

        column_list = ", ".join(quote(name) for name in columns)
        cursor.execute(
            f"INSERT INTO {quote(target)} ({column_list}) "
            f"SELECT {column_list} FROM {quote(staging)}"
        )
        logger.info(
            "%s: merged %d row(s), replacing %d existing",
            target, staged, max(replaced, 0),
        )
        return staged
    finally:
        cursor.execute(f"DROP TABLE IF EXISTS {quote(staging)}")


@dataclass
class LoadResult:
    entity_set: str
    table: str
    status: str
    rows: int = 0
    seconds: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_SUCCEEDED, STATUS_SKIPPED)


def latest_prefix(storage: Storage, spec: IngestSpec, *, root: str) -> str | None:
    """The most recent dated folder holding a manifest for this set.

    Chosen by name rather than by modification time: the folder names are
    ISO dates, so lexical order is chronological order, and a file copied
    between accounts keeps its name while losing its timestamps.
    """
    # No trailing slash on what is handed to list(): the port validates its
    # prefix as a key, and a trailing slash is an empty path segment, which is
    # refused. Matching still uses the slash-terminated form, so a set whose
    # name is a prefix of another -- MaterialSet against MaterialSetX -- cannot
    # pick up the other's runs.
    base = f"{root.strip('/')}/{spec.service}/{spec.name}"
    marker = f"{base}/"
    suffix = f"/{MANIFEST_FILE}"
    dates = sorted(
        key[len(marker) : -len(suffix)]
        for key in storage.list(base)
        if key.startswith(marker) and key.endswith(suffix)
    )
    return f"{marker}{dates[-1]}" if dates else None


def latest_prefixes(storage: Storage, *, root: str) -> dict[str, str]:
    """Latest landed prefix for every entity set, from a single listing.

    ``latest_prefix`` asks per set, which is right when loading one. For a
    listing of all 21 that is 21 round trips to the Data Lake to answer one
    question, so this walks the whole prefix once and groups in memory.
    """
    suffix = f"/{MANIFEST_FILE}"
    best: dict[str, str] = {}
    for key in storage.list(root.strip("/")):
        if not key.endswith(suffix):
            continue
        prefix = key[: -len(suffix)]
        parts = prefix.split("/")
        if len(parts) < 4:
            # Not <root>/<service>/<set>/<date>; something else lives here.
            continue
        name = parts[-2]
        # Lexical comparison is chronological: only the dated final segment
        # differs between runs of the same set, and it is ISO.
        if name not in best or prefix > best[name]:
            best[name] = prefix
    return best


def _iter_rows(
    storage: Storage, key: str, columns: list[str]
) -> Iterator[tuple]:
    """Stream the JSONL file as tuples in column order.

    A generator, not a list: this feeds ``fast_executemany`` in batches and
    memory stays flat whatever the file size.

    ``None`` is preserved rather than coerced to an empty string. The raw layer
    is meant to be a faithful copy, and "SAP sent no value" is different from
    "SAP sent a blank" -- a distinction the normalise step may well need.
    """
    with storage.open_read(key) as source:
        for raw in source:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            row = json.loads(line)
            yield tuple(row.get(column) for column in columns)


def load_set(
    spec: IngestSpec,
    *,
    root: str,
    prefix: str | None = None,
    storage: Storage | None = None,
    allow_unstable: bool = False,
) -> LoadResult:
    """Load one landed fetch into ``odata_<table>``. Never raises."""
    storage = storage or get_storage()
    started = time.monotonic()
    started_at = datetime.now(timezone.utc)

    def failure(detail: str) -> LoadResult:
        logger.error("%s: %s", spec.name, detail)
        return LoadResult(
            spec.name,
            spec.raw_table,
            STATUS_FAILED,
            seconds=time.monotonic() - started,
            error=detail,
        )

    if prefix is None:
        prefix = latest_prefix(storage, spec, root=root)
        if prefix is None:
            return failure(
                f"nothing landed for {spec.name} under {root}. "
                "Run the fetch stage first: python -m app.ingest --fetch"
            )

    try:
        manifest = read_manifest(storage, prefix)
    except Exception as exc:
        return failure(f"could not read the run manifest at {prefix}: {exc}")

    if not manifest.get("usable", False) and not allow_unstable:
        return failure(
            f"the fetch at {prefix} is marked unusable "
            f"(duplicate_keys={manifest.get('duplicate_keys')}, "
            f"error={manifest.get('error')!r}). Refusing to load it: the rows "
            "would look complete and not be. Re-fetch, or pass --allow-unstable "
            "if you are deliberately inspecting a bad pull."
        )

    columns = list(manifest.get("columns") or [])
    if not columns:
        return failure(f"the run manifest at {prefix} records no columns")

    expected = manifest.get("rows")
    data_key = f"{prefix}/{manifest.get('data_file', DATA_FILE)}"
    strategy = manifest.get("load_strategy", "replace")

    try:
        # Keys are the columns anything downstream will join on, and they are
        # the only ones given a bounded width at CREATE time -- SQL Server
        # cannot index nvarchar(max).
        writer = RawTableWriter(tuple(spec.keys))

        with get_engine().begin() as connection:
            raw_connection = connection.connection.driver_connection
            # try/finally rather than `with`: pyodbc's cursor context manager
            # does more on exit than close. The engine.begin() owns the
            # transaction, so a failure here rolls the table back to what it
            # held before.
            cursor = raw_connection.cursor()
            try:
                merging = strategy == "merge" and _table_exists(
                    cursor, spec.raw_table
                )
                if strategy == "merge" and not merging:
                    # A delta with nothing to merge into. Loading it as a
                    # replace would leave a table holding only the increment
                    # and looking complete, which is the worst of both.
                    return failure(
                        f"{spec.raw_table} does not exist, so this delta has "
                        "nothing to merge into. Run a full pull first: "
                        f"python -m app.ingest --fetch --load --full "
                        f"--set {spec.name}"
                    )

                if merging and not expected:
                    # Nothing changed in the window. Staging an empty batch
                    # would still compare shapes, and a table carrying a
                    # column SAP has since stopped sending would fail a
                    # merge of nothing at all.
                    rows = 0
                    logger.info(
                        "%s: the delta carried no rows; %s is unchanged",
                        spec.name, spec.raw_table,
                    )
                elif merging:
                    rows = _merge(
                        cursor,
                        writer,
                        spec,
                        columns,
                        _iter_rows(storage, data_key, columns),
                    )
                else:
                    writer.create_table(cursor, spec.raw_table, columns)
                    rows = writer.bulk_load(
                        cursor,
                        spec.raw_table,
                        columns,
                        _iter_rows(storage, data_key, columns),
                    )
                    # After the rows, never before: maintaining an index during
                    # a bulk load costs more than building it once at the end.
                    for key_column in spec.keys:
                        if key_column in columns:
                            writer.create_index(cursor, spec.raw_table, key_column)
            finally:
                cursor.close()
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        _record(spec, 0, STATUS_FAILED, started_at, prefix, error=detail)
        return failure(f"load failed -- {detail}")

    if expected is not None and rows != expected:
        # Not fatal, but it means the file and its manifest disagree, which
        # should never happen and points at a partial write.
        logger.error(
            "%s: loaded %d rows but the manifest recorded %d. The landed file "
            "and its manifest disagree -- treat this table as suspect.",
            spec.name,
            rows,
            expected,
        )

    elapsed = time.monotonic() - started
    _record(spec, rows, STATUS_SUCCEEDED, started_at, prefix)
    logger.info(
        "%s: %d rows into %s in %.1fs", spec.name, rows, spec.raw_table, elapsed
    )
    return LoadResult(spec.name, spec.raw_table, STATUS_SUCCEEDED, rows, elapsed)


def _record(
    spec: IngestSpec,
    rows: int,
    status: str,
    started_at: datetime,
    prefix: str,
    *,
    error: str | None = None,
) -> None:
    """Write the audit row. Best effort -- if the database is what failed, so is this."""
    try:
        session_factory = get_sessionmaker()
        with session_factory() as session:
            # Supersede earlier rows for this table so the audit shows current
            # state rather than an ever-growing history of reloads.
            session.query(IngestionRun).filter(
                IngestionRun.target_table == spec.raw_table
            ).delete(synchronize_session=False)
            session.add(
                IngestionRun(
                    source_file=prefix,
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
        logger.exception("%s: could not record the ingestion run", spec.name)

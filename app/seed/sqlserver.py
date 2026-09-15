"""Writing the raw layer into Azure SQL.

Everything else in this system is portable SQLAlchemy. This is the one place
that talks to the driver directly, and the reason is throughput: loading 3.3
million rows through the ORM, or through one INSERT per row, is hours rather
than minutes.

SQL Server has no ``COPY``. The closest equivalent reachable from Python is
pyodbc's ``fast_executemany``, which packs a batch of parameter sets into a
single round trip. That is what this module is built around.

Two details are worth knowing before changing anything here.

**Indexed columns cannot be ``nvarchar(max)``.** SQL Server caps an index key at
900 bytes, so any column that will carry an index is created at a bounded width
and the rest are unbounded. Get this wrong and the load succeeds while
``CREATE INDEX`` fails at the very end, after all the rows are in.

**The table description is an extended property, not a comment.** It carries the
warning a person sees when they find a raw table in SSMS and start writing SQL
against it, which is exactly the moment the raw layer's shape is least obvious.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

# Rows per round trip. Large enough that per-batch overhead disappears, small
# enough that one batch's parameters stay a reasonable amount of memory.
BATCH_ROWS = 1000

# The widest index key SQL Server accepts is 900 bytes; 450 nchars always fits,
# and is an order of magnitude more than the widest SAP key field we index
# (CDHDR's OBJECTID, at 90).
INDEXED_COLUMN_CHARS = 450


def quote(identifier: str) -> str:
    """Bracket-quote an identifier, escaping any closing bracket.

    Written out rather than borrowed from SQLAlchemy because these statements go
    to the driver directly, below the level where SQLAlchemy would quote them.
    Column names come from spreadsheet headers, so they are not trusted input
    even though they are not attacker-controlled.
    """
    return "[" + identifier.replace("]", "]]") + "]"


class RawTableWriter:
    """Creates, describes, indexes and fills one raw table."""

    def __init__(self, indexed_columns: tuple[str, ...] = ()) -> None:
        # Needed at CREATE time, not just at index time: a column that will carry
        # an index has to be given a bounded width up front.
        self._indexed = set(indexed_columns)

    def _column_type(self, column: str) -> str:
        if column in self._indexed:
            return f"nvarchar({INDEXED_COLUMN_CHARS})"
        return "nvarchar(max)"

    def create_table(self, cursor: Any, table: str, columns: list[str]) -> None:
        """Drop and recreate ``table`` with one text column per name.

        Drop-and-create rather than ``CREATE IF NOT EXISTS``: the table is fully
        reloaded anyway, and this absorbs a changed extract shape instead of
        failing on a column that no longer exists. It runs inside the caller's
        transaction, so a failed load leaves the previous table intact.
        """
        quoted = quote(table)
        # DROP TABLE IF EXISTS needs SQL Server 2016+; Azure SQL is always newer.
        cursor.execute(f"DROP TABLE IF EXISTS {quoted}")
        definitions = ", ".join(
            f"{quote(name)} {self._column_type(name)}" for name in columns
        )
        cursor.execute(f"CREATE TABLE {quoted} ({definitions})")

    def describe_table(self, cursor: Any, table: str, description: str) -> None:
        """Set MS_Description, SQL Server's equivalent of a table comment.

        The table was just dropped and recreated, so any previous property went
        with it and adding is always the right call. Failure is logged and
        swallowed: losing the warning is bad, but losing 3.3 million rows over a
        cosmetic property would be worse.
        """
        try:
            cursor.execute(
                "EXEC sp_addextendedproperty "
                "@name = N'MS_Description', @value = ?, "
                "@level0type = N'SCHEMA', @level0name = N'dbo', "
                "@level1type = N'TABLE',  @level1name = ?",
                description,
                table,
            )
        except Exception as exc:  # pragma: no cover - needs a live SQL Server
            logger.warning("%s: could not set the table description: %s", table, exc)

    def create_index(self, cursor: Any, table: str, column: str) -> None:
        """Index one column. Called after the rows are in, never before."""
        cursor.execute(
            f"CREATE INDEX {quote(f'ix_{table}_{column}')} "
            f"ON {quote(table)} ({quote(column)})"
        )

    def bulk_load(
        self, cursor: Any, table: str, columns: list[str], rows: Iterable[tuple]
    ) -> int:
        """Load ``rows`` into ``table``. Returns the row count.

        ``rows`` is a generator streaming from the workbook and is consumed in
        batches, never materialised: the largest extract is 143 MB and several
        exceed a million rows.
        """
        placeholders = ", ".join("?" for _ in columns)
        column_list = ", ".join(quote(name) for name in columns)
        statement = f"INSERT INTO {quote(table)} ({column_list}) VALUES ({placeholders})"

        fast = _enable_fast_executemany(cursor, len(columns))

        count = 0
        for batch in _batched(rows, BATCH_ROWS):
            if fast:
                try:
                    cursor.executemany(statement, batch)
                except Exception as exc:
                    # Do not lose the load over a binding problem. Turn the
                    # optimisation off, redo this batch the slow way, and say so
                    # loudly -- a seed that takes an hour longer is recoverable,
                    # a seed that dies at table 19 of 27 is not.
                    logger.warning(
                        "%s: fast_executemany failed (%s: %s); continuing without it. "
                        "The load will be considerably slower.",
                        table,
                        type(exc).__name__,
                        exc,
                    )
                    fast = False
                    cursor.fast_executemany = False
                    cursor.executemany(statement, batch)
            else:
                cursor.executemany(statement, batch)
            count += len(batch)
        return count


def _enable_fast_executemany(cursor: Any, column_count: int) -> bool:
    """Turn on pyodbc's batched binding, defensively. Returns whether it is on.

    This is the single biggest performance lever in the seed -- without it
    pyodbc round-trips once per row and a 900k-row table takes hours -- and also
    the most likely thing to break against a real Azure SQL, which is why it is
    wrapped so carefully.

    The hazard is ``nvarchar(max)``. With ``fast_executemany`` pyodbc asks the
    server to describe each parameter and pre-allocates a buffer per parameter
    per row. A ``max`` column has no meaningful declared width, which produces
    either "Invalid precision value" (HY104) or an allocation far larger than
    intended. ``setinputsizes`` with a size of 0 tells pyodbc to treat the
    column as a streamed long value instead, which is the documented way out.

    Both steps are best-effort. If either fails the caller falls back to
    ordinary ``executemany``, which is slow and correct.
    """
    try:
        import pyodbc
    except ImportError:  # pragma: no cover - pyodbc is a declared dependency
        logger.warning("pyodbc not available; loading without fast_executemany.")
        return False

    try:
        # Size 0 == "stream this as a long value", which is what makes
        # nvarchar(max) safe to bind in a batch.
        cursor.setinputsizes([(pyodbc.SQL_WVARCHAR, 0, 0)] * column_count)
        cursor.fast_executemany = True
        return True
    except Exception as exc:  # pragma: no cover - driver-dependent
        logger.warning(
            "Could not enable fast_executemany (%s: %s); the load will be slow.",
            type(exc).__name__,
            exc,
        )
        try:
            cursor.fast_executemany = False
        except Exception:
            pass
        return False


def _batched(rows: Iterable[tuple], size: int) -> Iterator[list[tuple]]:
    """Yield lists of at most ``size`` rows, without buffering the whole input."""
    batch: list[tuple] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch

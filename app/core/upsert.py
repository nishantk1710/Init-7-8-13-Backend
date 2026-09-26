"""Portable batch upsert on a natural key.

Idempotency has to be enforced by the database: an application-level
"does it exist?" check races with a concurrent run and silently produces
duplicates. Each engine has its own atomic form, so this is the one place that
knows about them:

* **SQL Server / Azure SQL** (what we deploy): ``MERGE ... WITH (HOLDLOCK)``.
  HOLDLOCK takes a key-range lock for the statement, which is what makes MERGE
  safe against a concurrent insert of the same key.
* **Postgres** (older local set-ups): ``INSERT ... ON CONFLICT DO UPDATE``.
* **Anything else**: look the keys up, then update or insert. Not race-safe,
  but the unique constraint on the natural key still turns a race into an
  error rather than a duplicate.

Callers size their batches with :func:`safe_batch_size`, because each engine
caps the number of bind parameters one statement may carry, and a multi-row
write binds ``rows x columns`` of them.
"""

from typing import Any

from sqlalchemy import insert, select, text, tuple_, update
from sqlalchemy.orm import Session

MAX_BIND_PARAMETERS = {
    "postgresql": 65535,
    # SQL Server's hard limit is 2,100 per request; leave headroom for the
    # driver's own parameters.
    "mssql": 2000,
    "sqlite": 32766,
}
"""Bind-parameter ceiling per statement, by SQLAlchemy dialect name."""

_CONSERVATIVE_MAX = min(MAX_BIND_PARAMETERS.values())


def safe_batch_size(model: type, requested: int, dialect_name: str | None = None) -> int:
    """Largest batch of ``model`` rows that stays under the dialect's ceiling.

    With no dialect, assumes the strictest engine we support.
    """
    ceiling = MAX_BIND_PARAMETERS.get(dialect_name or "", _CONSERVATIVE_MAX)
    return max(1, min(requested, ceiling // len(model.__table__.columns)))


def upsert(session: Session, model: type, rows: list[dict[str, Any]], conflict: list[str]) -> None:
    """Insert ``rows``, updating every other non-``id`` column when the
    ``conflict`` key already exists."""
    if not rows:
        return
    dialect = session.get_bind().dialect.name
    if dialect == "mssql":
        _merge_mssql(session, model, rows, conflict)
    elif dialect == "postgresql":
        _on_conflict_postgres(session, model, rows, conflict)
    else:
        _lookup_then_write(session, model, rows, conflict)


def _updatable(model: type, rows: list[dict[str, Any]], conflict: list[str]) -> list[str]:
    return [
        column.name
        for column in model.__table__.columns
        if column.name in rows[0] and column.name not in conflict and column.name != "id"
    ]


def _merge_mssql(session: Session, model: type, rows: list[dict[str, Any]], conflict: list[str]) -> None:
    table = model.__table__
    dialect = session.get_bind().dialect
    preparer = dialect.identifier_preparer
    columns = [column for column in table.columns if column.name in rows[0]]
    names = [column.name for column in columns]

    # Every placeholder is CAST to its column type. Without it pyodbc cannot
    # describe parameters inside a VALUES list and binds NULLs as varbinary,
    # which SQL Server then refuses to convert to int/date/decimal.
    params: dict[str, Any] = {}
    value_rows = []
    for r, row in enumerate(rows):
        cells = []
        for c, column in enumerate(columns):
            key = f"p{r}_{c}"
            params[key] = row.get(column.name)
            cells.append(f"CAST(:{key} AS {column.type.compile(dialect=dialect)})")
        value_rows.append(f"({', '.join(cells)})")

    quoted = {name: preparer.quote(name) for name in names}
    on = " AND ".join(f"target.{quoted[name]} = source.{quoted[name]}" for name in conflict)
    updatable = _updatable(model, rows, conflict)
    column_list = ", ".join(quoted[name] for name in names)

    statement = (
        f"MERGE INTO {preparer.format_table(table)} WITH (HOLDLOCK) AS target "
        f"USING (VALUES {', '.join(value_rows)}) AS source ({column_list}) "
        f"ON {on} "
    )
    if updatable:
        assignments = ", ".join(f"target.{quoted[name]} = source.{quoted[name]}" for name in updatable)
        statement += f"WHEN MATCHED THEN UPDATE SET {assignments} "
    statement += (
        f"WHEN NOT MATCHED THEN INSERT ({column_list}) "
        f"VALUES ({', '.join(f'source.{quoted[name]}' for name in names)});"
    )
    session.execute(text(statement), params)


def _on_conflict_postgres(
    session: Session, model: type, rows: list[dict[str, Any]], conflict: list[str]
) -> None:
    from sqlalchemy.dialects.postgresql import insert as postgres_insert

    statement = postgres_insert(model).values(rows)
    updatable = {name: statement.excluded[name] for name in _updatable(model, rows, conflict)}
    if updatable:
        statement = statement.on_conflict_do_update(index_elements=conflict, set_=updatable)
    else:
        statement = statement.on_conflict_do_nothing(index_elements=conflict)
    session.execute(statement)


def _lookup_then_write(
    session: Session, model: type, rows: list[dict[str, Any]], conflict: list[str]
) -> None:
    table = model.__table__
    key_columns = [table.c[name] for name in conflict]
    keys = {tuple(row[name] for name in conflict) for row in rows}
    existing = {
        tuple(found[1:]): found[0]
        for found in session.execute(
            select(table.c.id, *key_columns).where(tuple_(*key_columns).in_(keys))
        )
    }
    updatable = _updatable(model, rows, conflict)
    for row in rows:
        row_id = existing.get(tuple(row[name] for name in conflict))
        if row_id is None:
            session.execute(insert(table).values(row))
        elif updatable:
            session.execute(
                update(table).where(table.c.id == row_id).values({name: row[name] for name in updatable})
            )

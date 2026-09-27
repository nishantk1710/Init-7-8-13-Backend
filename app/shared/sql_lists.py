"""List predicates that run on SQL Server as well as Postgres.

Postgres lets a query bind a Python list as one array and test it with
``= any(:values)`` or ``like any(:patterns)``. SQL Server has no array type, so
neither exists there, and the replacements have to be spelled out:

* **A list membership** becomes an expanding ``IN`` -- one bind parameter per
  value. SQL Server refuses a statement with more than 2,100 parameters, so a
  long list is sent in chunks and the rows are concatenated. That is only
  correct when every row belongs to exactly one value of the list, which is
  true for a filter on a key column; :func:`fetch_in_chunks` says so rather
  than enforcing it, because only the query knows.
* **A pattern list** becomes ``(col like :p_0 or col like :p_1 ...)``, each
  pattern still a bind parameter, so configuration never reaches the SQL text.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

#: Values per chunk. Well under SQL Server's 2,100 bind parameters, leaving room
#: for the query's own.
IN_CHUNK = 1000

#: The escape character :func:`like_literal` uses and :func:`like_any` declares.
LIKE_ESCAPE = "\\"


def chunked(values: Iterable[Any], size: int = IN_CHUNK) -> Iterator[list[Any]]:
    """``values`` in lists of at most ``size``, in order."""
    chunk: list[Any] = []
    for value in values:
        chunk.append(value)
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def fetch_in_chunks(
    db: Session,
    sql: str,
    name: str,
    values: Sequence[Any],
    params: Mapping[str, Any] | None = None,
) -> list[RowMapping]:
    """Run ``sql`` -- which tests ``... in :name`` -- once per chunk of ``values``.

    Concatenating chunks is right only when each result row is determined by
    one value of the list: a filter on the column the query groups by, as every
    caller does. A query aggregating ACROSS list values would need its
    aggregate finished in Python instead.

    An empty list runs nothing and returns nothing, which is what ``in ()``
    would mean.
    """
    statement = text(sql).bindparams(bindparam(name, expanding=True))
    rows: list[RowMapping] = []
    for chunk in chunked(values):
        rows.extend(db.execute(statement, {**(params or {}), name: chunk}).mappings().all())
    return rows


def like_literal(value: str) -> str:
    """``value`` with every LIKE wildcard escaped, so it matches only itself.

    Escapes ``[`` too: on SQL Server it opens a character class.
    """
    for character in (LIKE_ESCAPE, "%", "_", "["):
        value = value.replace(character, LIKE_ESCAPE + character)
    return value


def like_any(column: str, name: str, patterns: Sequence[str]) -> tuple[str, dict[str, str]]:
    """``(sql, params)`` for ``column`` matching any of ``patterns``.

    The fragment is parenthesised, so it can be ANDed as it stands. No patterns
    means nothing matches -- ``1 = 0`` -- rather than an invalid ``()``.
    """
    if not patterns:
        return "1 = 0", {}
    params = {f"{name}_{index}": pattern for index, pattern in enumerate(patterns)}
    clauses = " or ".join(f"{column} like :{key} escape '{LIKE_ESCAPE}'" for key in params)
    return f"({clauses})", params

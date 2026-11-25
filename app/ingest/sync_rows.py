"""Shared helpers for the jobs that re-read rows by key and hold them against
``raw_<table>``: the open-documents re-read (``app.ingest.open_docs``) and the
sync audit (``app.ingest.sync_audit``).

Nothing here writes. It reads a landed fetch back, reads our rows for a set of
document keys, and says which rows of one side the other lacks -- matched on
the set's identity key, compared the way the ``n_<table>`` views read keys
(leading zeros dropped), because a raw table can hold a key padded by one route
and unpadded by another.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.core.logging import get_logger
from app.ingest.manifest import IngestSpec

logger = get_logger(__name__)

# Keys per IN list. Each key travels twice (as stored and zero-padded), and SQL
# Server allows 2,100 parameters a statement.
DB_KEY_BATCH = 500

# Rows named in a log line or a summary, at most.
SAMPLE_LIMIT = 5


def norm_key(value: Any) -> str:
    """A key value as the views compare it: trimmed, and an all-digit value
    without its leading zeros ('0002061710' and '2061710' are one reservation)."""
    text = "" if value is None else str(value).strip()
    if text.isdigit():
        return text.lstrip("0") or "0"
    return text


def landed_rows(storage, prefix: str) -> list[dict[str, Any]]:
    """The rows of a landed fetch, as the merge reads them. [] for an empty read."""
    from app.ingest.fetch import DATA_FILE, read_manifest

    manifest = read_manifest(storage, prefix)
    if not manifest.get("rows"):
        return []
    rows: list[dict[str, Any]] = []
    with storage.open_read(f"{prefix}/{manifest.get('data_file', DATA_FILE)}") as source:
        for raw in source:
            line = raw.decode("utf-8").strip()
            if line:
                rows.append(json.loads(line))
    return rows


@dataclass
class RawShape:
    """How one set's rows sit in its raw table: the table, the vocabulary, the
    column behind each OData property, and the identity key in both spellings."""

    table: str
    vocabulary: str | None
    columns: dict[str, str]              # OData property -> raw column
    numeric: dict[str, bool]             # OData property -> the view reads it as a number
    edm: dict[str, str]                  # OData property -> Edm type
    identity_props: list[str] = field(default_factory=list)
    identity_columns: list[str] = field(default_factory=list)

    def column_for(self, prop: str) -> str | None:
        return self.columns.get(prop)


def raw_shape(spec: IngestSpec, engine) -> RawShape | None:
    """The raw table behind ``spec`` and how its columns line up, or None when
    there is no such table (yet) or its key columns cannot be found."""
    from sqlalchemy import inspect

    from app.ingest.odata_tables import OdataTable
    from app.ingest.raw_merge import raw_table_for, resolve

    target = raw_table_for(spec)
    if target is None:
        return None
    table = target.raw_table
    inspector = inspect(engine)
    if not inspector.has_table(table):
        return None
    present = [c["name"] for c in inspector.get_columns(table)]
    # Exactly the merge's own call (raw_merge.merge): the SAP table's name for
    # the normalise mapping, aliases only for a table an OData set alone fills.
    vocabulary, resolved, missing = resolve(
        spec, target.table, present, aliases=isinstance(target, OdataTable)
    )
    if missing:
        logger.warning("%s: %s has no column for key(s) %s", spec.name, table, ", ".join(missing))
        return None
    shape = RawShape(
        table=table,
        vocabulary=vocabulary,
        columns={r.prop: r.column for r in resolved if not r.added},
        numeric={r.prop: r.numeric for r in resolved},
        edm={r.prop: r.edm_type for r in resolved},
    )
    by_prop = {r.prop: r for r in resolved}
    for prop in spec.identity_keys:
        found = by_prop.get(prop)
        if found is None or found.added:
            return None
        shape.identity_props.append(prop)
        shape.identity_columns.append(found.column)
    return shape


def key_variants(values: Iterable[str], spec: IngestSpec, key_prop: str) -> list[str]:
    """Each key as stored by either route: as given, zero-stripped, and padded
    to the property's declared length (``refresh.pad_key``)."""
    from app.ingest.refresh import pad_key

    out: dict[str, None] = {}
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        out.setdefault(text, None)
        out.setdefault(norm_key(text), None)
        out.setdefault(pad_key(text, spec, key_prop), None)
    return list(out)


def db_rows_for_keys(
    engine, shape: RawShape, key_column: str, keys: list[str], columns: list[str]
) -> list[dict[str, Any]]:
    """Our rows whose ``key_column`` is one of ``keys`` (each in every stored
    spelling), with ``columns``. An index seek: every raw table carries an index
    led by its document key (``csv_load``, KEY INDEX)."""
    from sqlalchemy import text

    from app.seed.sqlserver import quote

    wanted = list(dict.fromkeys(columns))
    select = ", ".join(quote(c) for c in wanted)
    rows: list[dict[str, Any]] = []
    with engine.connect() as connection:
        for start in range(0, len(keys), DB_KEY_BATCH):
            chunk = keys[start : start + DB_KEY_BATCH]
            names = [f"k{i}" for i in range(len(chunk))]
            sql = (
                f"SELECT {select} FROM {quote(shape.table)} "
                f"WHERE {quote(key_column)} IN ({', '.join(':' + n for n in names)})"
            )
            for row in connection.execute(text(sql), dict(zip(names, chunk))):
                rows.append(dict(zip(wanted, row)))
    return rows


def identity_of_sap_row(row: dict[str, Any], shape: RawShape) -> tuple[str, ...]:
    return tuple(norm_key(row.get(p)) for p in shape.identity_props)


def identity_of_db_row(row: dict[str, Any], shape: RawShape) -> tuple[str, ...]:
    return tuple(norm_key(row.get(c)) for c in shape.identity_columns)


def missing_in_sap(
    sap_rows: list[dict[str, Any]], db_rows: list[dict[str, Any]], shape: RawShape
) -> list[tuple[str, ...]]:
    """Identity keys of our rows that SAP did not return when asked for their
    document: deleted in SAP (or outside what the OData set exposes)."""
    returned = {identity_of_sap_row(r, shape) for r in sap_rows}
    seen: dict[tuple[str, ...], None] = {}
    for row in db_rows:
        identity = identity_of_db_row(row, shape)
        if identity not in returned:
            seen.setdefault(identity, None)
    return list(seen)

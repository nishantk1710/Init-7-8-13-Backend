"""Merge OData rows into the raw table the CSV full pull created.

    CSV full pull   ->  raw_<table>   the baseline: every column, SAP field names
    OData delta     ->  raw_<table>   the same table, updated in place  (this module)
                    ->  odata_<table> as before; nothing here changes that

WHY THE SAME TABLE

The initiatives read ``raw_<table>`` (through the ``n_<table>`` views). A delta
that lands anywhere else keeps a table fresh that nobody looks at. So the rows
an OData read returns -- an increment since the watermark, or a whole set used
as enrichment -- go into the table the pages read, and the watermark then means
what it says: the pages are current to it.

WHAT MAKES THAT SAFE

The OData row is NARROW and in OData's vocabulary; the raw table is WIDE and in
SAP's. EKPO over OData is 20 properties named ``Ebeln``, ``Aedat``; over CSV it
is 277 columns named ``EBELN``, ``AEDAT``. So:

* Existing rows are UPDATED column by column -- only the columns the OData set
  carries. Deleting and re-inserting would null the other 257.
* New rows are INSERTED with those columns filled and the rest NULL, until the
  next full pull fills them. That is the nature of a narrow delta and the
  reason the nightly full refresh exists.
* Rows are matched on the set's identity key, mapped to the table's columns,
  exactly. The incoming batch is reduced to one row per key first, so a chunked
  read that returned a row twice cannot insert it twice, and a second run of
  the same delta updates every row to itself and inserts nothing.
* A fetch marked unstable (duplicate keys measured across chunks) is refused,
  as it is for ``odata_<table>``.

THE VOCABULARY

An OData property maps to the SAP field behind it by name -- ``Ebeln`` is
``EBELN``, ``BudatMkpf`` is ``BUDAT_MKPF`` -- which is the CSV column name. A
table the workbook fallback filled instead carries labels (``purchasing_document``),
and ``app.shared.sap_normalise`` records which label each SAP field feeds, so the
same property resolves there too. Which vocabulary a table is in is read off its
key columns; every other property follows it. A property the table has no column
for at all gets one added (text, nullable) rather than being dropped -- the
``n_<table>`` view falls back to the SAP field name, so it becomes readable at
once.

Values are shaped to the vocabulary too. SAP serialises a DATS as local midnight;
the envelope decodes that to 22:00 UTC the evening before, so the calendar day
is the NEAREST one, not the UTC one. A CSV-shaped table gets ``DD.MM.YYYY``, a
workbook-shaped one ``YYYY-MM-DD``; both are what the normalise view expects.
Booleans become SAP's ``X``/blank. Numbers are written as text, as every raw
column is.

WHAT IS REFUSED, AND WHY

* No raw table yet: nothing to merge into. The CSV full pull builds it and sets
  the watermark; a delta before that has no baseline and is skipped, not failed.
* A key column the table lacks: rows cannot be matched, and inserting them
  unmatched is how duplicates start.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Column, Engine, MetaData, Table, UnicodeText, inspect, text

from app.core.db import get_engine
from app.core.logging import get_logger
from app.core.storage import Storage, get_storage
from app.ingest.csv_tables import CSV_TABLES, CsvTable
from app.ingest.manifest import IngestSpec
from app.seed.sqlserver import quote

logger = get_logger(__name__)

STAGING_SUFFIX = "__odata_stg"

VOCAB_SAP = "sap"            # CSV-shaped: SAP field names, DD.MM.YYYY
VOCAB_WORKBOOK = "workbook"  # seed/fallback-shaped: labels, YYYY-MM-DD

SKIPPED = "skipped"
MERGED = "merged"
REFUSED = "refused"


@dataclass
class MergeResult:
    table: str
    status: str
    updated: int = 0
    inserted: int = 0
    columns_added: list[str] = field(default_factory=list)
    rows_without_key: int = 0
    vocabulary: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (MERGED, SKIPPED)


# --- Names ------------------------------------------------------------------


def raw_table_for(spec: IngestSpec) -> CsvTable | None:
    """The CSV-route table holding the same data as this OData set, if any."""
    for table in CSV_TABLES:
        if table.entity_set == spec.name:
            return table
    return None


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def sap_field(odata_property: str) -> str:
    """``Ebeln`` -> ``EBELN``, ``BudatMkpf`` -> ``BUDAT_MKPF``, ``Txz01`` -> ``TXZ01``.

    Gateway names an OData property from the ABAP field by CamelCasing at the
    underscores; this is the inverse, and it is what the CSV header calls the
    same column.
    """
    return _CAMEL_BOUNDARY.sub("_", odata_property).upper()


def _labels_for(table: str, field_name: str) -> list[str]:
    """Workbook labels that this SAP field feeds, from the normalise mapping."""
    from app.shared.sap_normalise import TABLES

    return [col.label for col in TABLES.get(table, ()) if field_name in col.sap]


@dataclass(frozen=True)
class Resolved:
    prop: str
    column: str          # the table's column, with the table's spelling
    edm_type: str
    is_key: bool
    added: bool = False


def resolve(
    spec: IngestSpec, table: str, present: list[str]
) -> tuple[str | None, list[Resolved], list[str]]:
    """Map each OData property to a column of the raw table.

    Returns ``(vocabulary, resolved, missing_keys)``. The vocabulary is decided
    by the key columns: all of them must resolve, and the names they resolve to
    say whether the table is CSV-shaped or workbook-shaped. Every other
    property prefers that vocabulary and is added under its SAP field name when
    the table has neither spelling.
    """
    by_lower = {c.lower(): c for c in present}
    keys = set(spec.identity_keys)
    types = {p.name: p.type for p in spec.entity_set.properties}

    def find(candidates: list[str]) -> str | None:
        for candidate in candidates:
            hit = by_lower.get(candidate.lower())
            if hit is not None:
                return hit
        return None

    # 1. Keys decide the vocabulary.
    vocabulary: str | None = None
    missing_keys: list[str] = []
    key_hits: dict[str, str] = {}
    for prop in spec.identity_keys:
        sap = sap_field(prop)
        labels = _labels_for(table, sap)
        if (hit := find([sap])) is not None:
            key_hits[prop] = hit
            vocabulary = vocabulary or VOCAB_SAP
        elif (hit := find(labels)) is not None:
            key_hits[prop] = hit
            vocabulary = vocabulary or VOCAB_WORKBOOK
        else:
            missing_keys.append(prop)
    if missing_keys:
        return vocabulary, [], missing_keys

    # 2. Everything else follows it.
    resolved: list[Resolved] = []
    for prop in (p.name for p in spec.entity_set.properties):
        sap = sap_field(prop)
        labels = _labels_for(table, sap)
        order = [sap, *labels] if vocabulary == VOCAB_SAP else [*labels, sap]
        if prop in key_hits:
            resolved.append(Resolved(prop, key_hits[prop], types.get(prop, "Edm.String"), True))
            continue
        hit = find(order)
        if hit is not None:
            resolved.append(Resolved(prop, hit, types.get(prop, "Edm.String"), False))
        else:
            resolved.append(Resolved(prop, sap, types.get(prop, "Edm.String"), False, added=True))
    return vocabulary, resolved, []


# --- Values -----------------------------------------------------------------


def _nearest_day(moment: datetime) -> date:
    """SAP serialises a DATS as local midnight; decoded to UTC that is up to a
    few hours either side of the day. Rounding to the nearest midnight gives
    the calendar day back for any zone within twelve hours of UTC."""
    if moment.tzinfo is not None:
        moment = moment.replace(tzinfo=None)
    return (moment + timedelta(hours=12)).date()


def shape(value: Any, edm_type: str, vocabulary: str) -> str | None:
    """One OData value as the raw table's text, in the table's vocabulary."""
    if value is None:
        return None
    if edm_type in ("Edm.DateTime", "Edm.DateTimeOffset"):
        if isinstance(value, datetime):
            day = _nearest_day(value)
        elif isinstance(value, date):
            day = value
        else:
            raw = str(value).strip()
            if not raw:
                return None
            try:
                day = _nearest_day(datetime.fromisoformat(raw))
            except ValueError:
                return raw  # not a date we understand; kept verbatim, never guessed
        return f"{day:%d.%m.%Y}" if vocabulary == VOCAB_SAP else day.isoformat()
    if edm_type == "Edm.Boolean" or isinstance(value, bool):
        truthy = value is True or str(value).strip().lower() in ("true", "x", "1")
        return "X" if truthy else ""
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


# --- The merge ----------------------------------------------------------------


def _table_exists(engine: Engine, table: str) -> bool:
    return inspect(engine).has_table(table)


def _column_type_sql(engine: Engine) -> str:
    return "nvarchar(max)" if engine.dialect.name == "mssql" else "TEXT"


def _add_column_sql(engine: Engine, table: str, column: str) -> str:
    keyword = "ADD" if engine.dialect.name == "mssql" else "ADD COLUMN"
    return f"ALTER TABLE {quote(table)} {keyword} {quote(column)} {_column_type_sql(engine)} NULL"


def merge(
    spec: IngestSpec,
    rows: list[dict[str, Any]],
    *,
    engine: Engine | None = None,
) -> MergeResult:
    """Upsert OData rows into ``raw_<table>``. Never raises; the result says."""
    target_spec = raw_table_for(spec)
    if target_spec is None:
        return MergeResult(spec.raw_table, SKIPPED, detail="no CSV-route table holds this set")
    table = target_spec.raw_table
    engine = engine or get_engine()

    try:
        if not _table_exists(engine, table):
            return MergeResult(
                table, SKIPPED,
                detail=f"{table} does not exist yet; the CSV full pull creates it "
                       "(python -m app.ingest --csv-pull --csv-load --table "
                       f"{target_spec.sap_table})",
            )
        present = [c["name"] for c in inspect(engine).get_columns(table)]
        vocabulary, resolved, missing_keys = resolve(spec, target_spec.table, present)
        if missing_keys:
            return MergeResult(
                table, REFUSED, vocabulary=vocabulary,
                detail=f"{table} has no column for key(s) {', '.join(missing_keys)}; "
                       "rows cannot be matched, and unmatched inserts are how "
                       "duplicates start",
            )
        assert vocabulary is not None

        keys = [r for r in resolved if r.is_key]
        by_key: dict[tuple, dict[str, str | None]] = {}
        without_key = 0
        for row in rows:
            key = tuple(shape(row.get(k.prop), k.edm_type, vocabulary) for k in keys)
            if any(k in (None, "") for k in key):
                without_key += 1
                continue
            # Last one wins: a chunked read can hand back the same row twice.
            by_key[key] = {r.column: shape(row.get(r.prop), r.edm_type, vocabulary) for r in resolved}

        if not by_key:
            return MergeResult(table, MERGED, vocabulary=vocabulary, rows_without_key=without_key,
                               detail="no rows with a complete key")

        columns = [r.column for r in resolved]
        key_columns = [r.column for r in keys]
        added = [r.column for r in resolved if r.added]
        staging = f"{table}{STAGING_SUFFIX}"

        with engine.begin() as conn:
            for column in added:
                conn.execute(text(_add_column_sql(engine, table, column)))

            meta = MetaData()
            stage = Table(staging, meta, *[Column(c, UnicodeText) for c in columns])
            stage.drop(conn, checkfirst=True)
            stage.create(conn)
            conn.execute(stage.insert(), list(by_key.values()))

            on = " AND ".join(
                f"s.{quote(k)} = {quote(table)}.{quote(k)}" for k in key_columns
            )
            exists = f"EXISTS (SELECT 1 FROM {quote(staging)} s WHERE {on})"
            non_key = [c for c in columns if c not in key_columns]
            if non_key:
                assignments = ", ".join(
                    f"{quote(c)} = (SELECT s.{quote(c)} FROM {quote(staging)} s WHERE {on})"
                    for c in non_key
                )
                updated = conn.execute(
                    text(f"UPDATE {quote(table)} SET {assignments} WHERE {exists}")
                ).rowcount
            else:
                updated = conn.execute(
                    text(f"SELECT COUNT(*) FROM {quote(table)} WHERE {exists}")
                ).scalar() or 0

            column_list = ", ".join(quote(c) for c in columns)
            not_exists = " AND ".join(
                f"t.{quote(k)} = s.{quote(k)}" for k in key_columns
            )
            inserted = conn.execute(text(
                f"INSERT INTO {quote(table)} ({column_list}) "
                f"SELECT {column_list} FROM {quote(staging)} s "
                f"WHERE NOT EXISTS (SELECT 1 FROM {quote(table)} t WHERE {not_exists})"
            )).rowcount
            stage.drop(conn)

        if added:
            # The n_<table> view resolves columns when it is built; a column
            # that did not exist then is invisible until it is rebuilt.
            from app.shared import sap_normalise

            sap_normalise.refresh_after_load(target_spec.table)

        logger.info(
            "%s: %d row(s) updated, %d inserted into %s (%s vocabulary%s%s)",
            spec.name, max(updated, 0), max(inserted, 0), table, vocabulary,
            f"; added {', '.join(added)}" if added else "",
            f"; {without_key} row(s) without a key skipped" if without_key else "",
        )
        return MergeResult(
            table, MERGED, updated=max(updated, 0), inserted=max(inserted, 0),
            columns_added=added, rows_without_key=without_key, vocabulary=vocabulary,
        )
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        logger.error("%s: merge into %s failed -- %s", spec.name, table, detail)
        return MergeResult(table, REFUSED, detail=detail)


def merge_landed(
    spec: IngestSpec,
    prefix: str,
    *,
    storage: Storage | None = None,
    engine: Engine | None = None,
) -> MergeResult:
    """Merge a landed fetch (``<prefix>/data.jsonl``) into ``raw_<table>``.

    Refuses a fetch its own manifest marks unusable, exactly as the
    ``odata_<table>`` load does: rows were lost or repeated in paging, and
    a merge would carry that into the table the pages read.
    """
    from app.ingest.fetch import DATA_FILE, read_manifest

    storage = storage or get_storage()
    try:
        manifest = read_manifest(storage, prefix)
    except Exception as exc:
        return MergeResult(spec.raw_table, REFUSED, detail=f"could not read the manifest at {prefix}: {exc}")
    if not manifest.get("usable", False):
        return MergeResult(
            spec.raw_table, REFUSED,
            detail=f"the fetch at {prefix} is marked unusable "
                   f"(duplicate_keys={manifest.get('duplicate_keys')}, error={manifest.get('error')!r})",
        )
    rows: list[dict[str, Any]] = []
    with storage.open_read(f"{prefix}/{manifest.get('data_file', DATA_FILE)}") as source:
        for raw in source:
            line = raw.decode("utf-8").strip()
            if line:
                rows.append(json.loads(line))
    return merge(spec, rows, engine=engine)

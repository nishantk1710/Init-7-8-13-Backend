"""SAP raw layer -> canonical staging, through the normalise views.

The only module in I07 that reads SAP data. Everything downstream reads the
staging tables or the canonical contracts built from them.

**It reads ``n_<table>``, never ``raw_<table>``.** The raw layer is filled by two
loaders that disagree on everything but the data -- the July workbooks (business
labels, unpadded keys, ISO dates) and the live SAP CSV extract (SAP field names,
zero-padded keys, ``DD.MM.YYYY`` dates, German decimal commas). The shared
normalise views (:mod:`app.shared.sap_normalise`) present both in the workbook
vocabulary, with keys unpadded, dates ISO and numbers plain, so the queries here
are written once and work whichever loader filled a table -- including a mix,
such as live EKPO beside fallback-workbook EKKO.

**MARC's MRP fields are the exception.** The live CSV MARC carries no DISMM,
PLIFZ, MINBE or MABST at all. When ``raw_marc`` lacks a field, it is taken from
``odata_material_plant`` (the OData MaterialPlantSet) instead -- decided per
field from the table's columns, never row by row.

**Aggregation happens in Python.** Monthly consumption, goods receipts and
schedule lines are totalled here rather than in ``GROUP BY``: every value is
text and has to be parsed, and parsing in Python is what makes a malformed value
a counted rejection instead of either a SQL error (SQL Server) or a silent
regex exclusion (the Postgres-only query this replaced). The SQL that remains is
plain joins and ``IN`` lists, portable to both dialects. The aggregates are one
entry per material-plant-month or PO line.

**Every read finishes before any write starts.** SQL Server refuses a statement
on a connection that still has an open result ("Connection is busy with results
for another command") unless MARS is on, which is not this driver's default. So
a read is either fetched whole (:func:`_read`) or streamed to completion
(:func:`_stream`) and only then upserted -- interleaving a streamed read with
batched MERGEs worked on Postgres and failed on the first batch on Azure SQL.

**What is streamed, and why.** The big reads -- movements (n_mseg), goods
receipts (n_ekbe) and schedule lines (n_eket) -- are folded into small
dictionaries by the aggregators below, one pass each, so there is no reason to
hold the raw rows: on the full extract they are over a million rows apiece, and
fetching them whole cost on the order of a gigabyte of Python objects inside the
web app's own container. Each stream is consumed completely by its aggregator
before the first write, so the rule above still holds. Reads that are written
while they are read (the purchase-order lines) stay whole, and are narrowed in
SQL to the plant scope instead.

**Idempotency is a database property.** Each staging table has a unique natural
key, and writes go through an atomic upsert (``MERGE`` on SQL Server, ``ON
CONFLICT`` on Postgres) -- so a second run converges on the same state instead
of appending duplicates, and two concurrent runs cannot interleave into one.
The dialect-specific SQL lives in one place, :mod:`app.core.upsert`.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Iterator

from sqlalchemy import bindparam, inspect, select, text, update
from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.core.upsert import safe_batch_size, upsert
from app.initiatives.i7.adapters.field_map import (
    CREDIT_INDICATOR,
    DELETION_FLAG_TRUE,
    GOODS_RECEIPT_HISTORY_CATEGORY,
    GOODS_RECEIPT_MOVEMENT_TYPE,
    ODATA_DELETION_FLAG_TRUE,
)
from app.initiatives.i7.adapters.ingestion_policy import ExtractIngestionPolicy
from app.initiatives.i7.adapters.validation import (
    RejectionReason,
    clean,
    first_of_month,
    parse_date,
    parse_decimal,
    parse_flag,
    parse_int,
    parse_purchasing_deletion,
)
from app.models.i7_staging import (
    StagedConsumption,
    StagedMaterial,
    StagedMaterialPlant,
    StagedPurchaseOrder,
    StagedStock,
    StagingRejection,
    StagingRun,
)
from app.shared import sap_normalise

logger = logging.getLogger(__name__)

SOURCE_NORMALISE_VIEWS = "normalise_views"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"


@dataclass
class StagingResult:
    """What one adapter run did."""

    run_id: int | None = None
    status: str = STATUS_SUCCEEDED
    materials: int = 0
    material_plants: int = 0
    stock: int = 0
    consumption: int = 0
    purchase_orders: int = 0
    deactivated: int = 0
    """Rows a complete-snapshot sweep marked inactive. Always 0 on a delta run."""
    rejections: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def rejected_total(self) -> int:
        return sum(self.rejections.values())


class _Rejections:
    """Collects rejections during a run and writes them once at the end."""

    def __init__(self, run_id: int) -> None:
        self._run_id = run_id
        self._rows: list[StagingRejection] = []
        self.counts: dict[str, int] = {}

    def add(self, source_table: str, source_key: str | None, reason: str, detail: str = "") -> None:
        self.counts[reason] = self.counts.get(reason, 0) + 1
        # Cap what is stored: the counts stay exact, but a systemic fault
        # affecting a million rows must not write a million rejection rows.
        if len(self._rows) < 5000:
            self._rows.append(
                StagingRejection(
                    staging_run_id=self._run_id,
                    source_table=source_table,
                    source_key=(source_key or "")[:255] or None,
                    reason=reason,
                    detail=detail[:500] or None,
                )
            )

    def flush(self, session: Session) -> None:
        if self._rows:
            session.bulk_save_objects(self._rows)
            self._rows = []


def _statement(statement: Any) -> Any:
    """A SQL string as a ``text()`` clause; a prepared clause passes through."""
    return text(statement) if isinstance(statement, str) else statement


def _read(session: Session, statement: Any, params: dict[str, Any] | None = None) -> list[Any]:
    """Run a query and fetch every row, closing its result before returning.

    Fetched whole on purpose, not streamed: the caller upserts on the same
    connection, and SQL Server will not run a MERGE while a result is still
    open on it. See the module docstring.
    """
    return session.execute(_statement(statement), params or {}).all()


#: Rows pulled from the driver per round trip by :func:`_stream`. Large enough
#: that the round trips do not dominate, small enough that a chunk is a few MB.
STREAM_CHUNK = 10_000


def _stream(
    session: Session, statement: Any, params: dict[str, Any] | None = None
) -> Iterator[Any]:
    """Rows of a query, a chunk at a time, closing the result when done.

    For a read an aggregator folds into a small dictionary in one pass, so the
    raw rows never have to exist all at once. The caller MUST consume it to the
    end before it writes: the result is held open until then, and SQL Server
    refuses a MERGE on a connection with an open result. Every use below hands
    the stream straight to an aggregator that exhausts it, which is what makes
    that true by construction rather than by care.

    ``finally`` closes the result even when the aggregator raises part-way, so
    a failure cannot leave the connection busy for the next statement.
    """
    result = session.execute(_statement(statement), params or {}).yield_per(STREAM_CHUNK)
    try:
        yield from result
    finally:
        result.close()


def _plant_scope_clause(column: str, scope: frozenset[str], *keys: str) -> str:
    """A SQL pre-filter that drops rows ``_out_of_scope`` would drop. Empty
    scope gives no clause, matching ``_out_of_scope``'s "no filter".

    It is a PRE-filter, not a replacement: every caller still applies
    ``_out_of_scope`` afterwards, so this only has to be no stricter than the
    Python test, never exactly as strict. Four details keep it so:

    * The value is trimmed first, as ``clean()`` trims it in Python, so a
      padded ``'1300 '`` still reaches the plant it names.
    * A NULL or blank plant is let THROUGH. The callers count those as
      ``MISSING_PLANT`` rejections, and a filter that dropped them would make
      malformed rows vanish from the counts instead of being reported.
    * So is a row missing any of ``keys`` -- the fields the callers reject on
      BEFORE they look at the plant (the material of a movement; the document
      and item of a PO line). A malformed row at an out-of-scope plant is
      counted today, and dropping it here would quietly change the counts.
    * Case and trailing blanks are compared the way SQL Server compares them
      (insensitively), which can only let more rows through to Python.

    ``column`` and ``keys`` are always literal column references in this module,
    never caller-supplied, which is why they are formatted in rather than bound.
    """
    if not scope:
        return ""
    trimmed = f"LTRIM(RTRIM({column}))"
    passthrough = " OR ".join(
        [f"NULLIF({trimmed}, '') IS NULL"]
        + [f"NULLIF(LTRIM(RTRIM({key})), '') IS NULL" for key in keys]
    )
    return f" AND ({passthrough} OR {trimmed} IN :plant_scope)"


def _scoped(statement: Any, scope: frozenset[str]) -> Any:
    """``statement`` with the expanding ``plant_scope`` bind declared, if the
    scope clause was added to it."""
    return statement.bindparams(bindparam("plant_scope", expanding=True)) if scope else statement


def _upper(value: str | None) -> str | None:
    """``value`` upper-cased; ``None`` stays ``None``."""
    return value.upper() if value is not None else None


def _out_of_scope(plant: str | None, scope: frozenset[str]) -> bool:
    """Whether ``plant`` falls outside the configured plant scope.

    Filtered here rather than in each SELECT because the five staging queries
    name the plant column differently (``plant``, ``p.plant``) and two are
    already parameterised; a WHERE clause per query is five places for the
    scope to drift out of step. An empty scope admits everything, so this is a
    no-op until ``I7_PLANTS`` is set.

    A row whose plant is NULL is NOT filtered here -- the callers already
    reject it as MISSING_PLANT with its own reason, and swallowing it as
    "out of scope" would hide a malformed row behind a business decision.
    """
    if not scope:
        return False
    if plant is None:
        return False
    return plant not in scope


def _safe_batch_size(model: type, requested: int, session: Session | None = None) -> int:
    """Largest batch that stays under the engine's bind-parameter ceiling.

    A multi-row write binds ``rows x columns`` parameters, so a batch size that
    is safe for a 6-column table overflows a 14-column one -- and SQL Server's
    ceiling (2,100) is far below Postgres's (65,535).
    """
    dialect = session.get_bind().dialect.name if session is not None else None
    return safe_batch_size(model, requested, dialect)


def _upsert(session: Session, model: type, rows: list[dict[str, Any]], conflict: list[str]) -> None:
    """Insert a batch, updating on natural-key conflict. See :mod:`app.core.upsert`."""
    upsert(session, model, rows, conflict)


def _batched(iterator: Iterator[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for row in iterator:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# --- Materials ---------------------------------------------------------

# MAKT can carry several languages per material (the live extract has English,
# Afrikaans and German); joining it straight would multiply MARA rows and put
# the same key into one upsert batch twice. English first, else any.
#
# Criticality is joined from ``criticality_current`` -- what the monthly ZMM065
# reports add up to (``app.integrations.criticality.reports``) -- with material
# numbers in the same stripped form the normalise views give n_mara.material.
# It is per material-plant there, and this staging table holds one value per
# material, so a material whose plants disagree takes its MOST SEVERE tier
# (``SEVERITY_ORDER``): the stock policy errs towards protecting a part that is
# critical anywhere. This replaces a plain MAX() over the text, which ranked the
# tiers alphabetically -- CRITICAL < IMPACT < INSURANCE < NORMAL < OBSOLETE --
# and so staged a part that is CRITICAL at one plant and NORMAL at the other as
# NORMAL, and anything OBSOLETE anywhere as OBSOLETE.
#
# Criticality may not be loaded at all (no report imported yet, or the
# migration has not run): __CRITICALITY__ is the real table, or an empty
# stand-in, so criticality is NULL rather than the staging failing -- see
# _material_sql.
_MATERIAL_SQL = """
    SELECT a.material,
           a.material_group,
           a.material_type,
           a.base_unit_of_measure,
           a.x_plant_matl_status,
           a.ext_material_group,
           a.manufacturer,
           a.df_at_client_level,
           COALESCE(t.material_description, a.material_description) AS material_description,
           z.criticality
      FROM n_mara a
      LEFT JOIN (
            SELECT material,
                   COALESCE(MAX(CASE WHEN language_key IN ('E', 'EN') THEN material_description END),
                            MAX(material_description)) AS material_description
              FROM n_makt
             GROUP BY material
      ) t ON t.material = a.material
      LEFT JOIN (
            SELECT material,
                   CASE MIN(CASE criticality __RANK__ END) __UNRANK__ END AS criticality
              FROM (__CRITICALITY__) u
             WHERE NULLIF(criticality, '') IS NOT NULL
             GROUP BY material
      ) z ON z.material = a.material
"""


def _material_sql(session: Session) -> str:
    """_MATERIAL_SQL over ``criticality_current``, or an empty stand-in when it
    does not exist yet."""
    from app.core.criticality import SEVERITY_ORDER
    from app.models.criticality_report import CriticalityCurrent
    from app.shared.optional_tables import existing

    table = CriticalityCurrent.__tablename__
    if existing(session, (table,)):
        source = f"SELECT material, criticality FROM {table}"
    else:
        logger.info("No criticality report is loaded; materials stage with no criticality")
        source = "SELECT CAST(NULL AS NVARCHAR(200)) AS material, CAST(NULL AS NVARCHAR(200)) AS criticality WHERE 1 = 0"
    # Tier names come from the enum, never from input.
    rank = " ".join(f"WHEN '{tier.value}' THEN {n}" for n, tier in enumerate(SEVERITY_ORDER, start=1))
    unrank = " ".join(f"WHEN {n} THEN '{tier.value}'" for n, tier in enumerate(SEVERITY_ORDER, start=1))
    return (
        _MATERIAL_SQL.replace("__CRITICALITY__", source)
        .replace("__RANK__", rank)
        .replace("__UNRANK__", unrank)
    )


# Price and currency come from wherever SAP supplied them, never assumed:
#
#   raw_mbew carries a moving price (the July workbook)  -> n_mbew.moving_price,
#       no currency column there, so currency stays NULL;
#   raw_mbew does not (the live CSV MBEW has no VERPR)   -> odata_material_valuation
#       (the OData MaterialValuationSet): Verpr per Peinh units, in Waers.
#
# The operating currency is ZAR by context, but writing that in would be
# inventing source data -- a currency is staged only when SAP stated one.

_PRICE_SQL = "SELECT material, moving_price FROM n_mbew"
ODATA_MATERIAL_VALUATION = "odata_material_valuation"


def _keep_highest(
    prices: dict[str, tuple[Decimal, str | None]], material: str, price: Decimal, currency: str | None
) -> None:
    """Highest price per material across valuation areas, with its own currency."""
    if material not in prices or price > prices[material][0]:
        prices[material] = (price, currency)


def _price_by_material(
    session: Session, rejections: _Rejections
) -> dict[str, tuple[Decimal, str | None]]:
    """``material -> (unit price, currency)``, the highest across valuation areas.

    Parsed before comparing: a MAX over the text column would rank "9.5" above
    "10.0". Read from MBEW when it carries a moving price, else from the OData
    valuation set (see the note above); empty when neither has one, so
    unit_price stages as NULL -- a visible gap.
    """
    present = _columns(session, "raw_mbew")
    mbew_has_price = present is not None and any(
        r.label == "moving_price" and r.source is not None
        for r in sap_normalise.resolve("mbew", present)
    )
    if not mbew_has_price:
        return _odata_prices(session, rejections)

    prices: dict[str, tuple[Decimal, str | None]] = {}
    for row in _read(session, _PRICE_SQL):
        material = clean(row.material)
        if material is None or clean(row.moving_price) is None:
            continue
        price = parse_decimal(row.moving_price)
        if price is None:
            rejections.add("n_mbew", material, RejectionReason.INVALID_QUANTITY, "moving price")
            continue
        _keep_highest(prices, material, price, None)
    return prices


def _odata_prices(session: Session, rejections: _Rejections) -> dict[str, tuple[Decimal, str | None]]:
    """Unit prices from the OData MaterialValuationSet: ``Verpr / Peinh`` in ``Waers``.

    VERPR is quoted per PEINH units (a price of 250.00 per 10 EA is 25.00 each),
    so the division is what makes it a unit price; a blank or zero PEINH is
    rejected rather than assumed to be 1. Empty, and logged, when the set has
    not been pulled -- ``python -m app.ingest --fetch --load --set
    MaterialValuationSet``.
    """
    present = _columns(session, ODATA_MATERIAL_VALUATION)
    if present is None:
        logger.warning(
            "raw_mbew has no moving price and %s does not exist: unit_price stages as NULL. "
            "Run: python -m app.ingest --fetch --load --set MaterialValuationSet",
            ODATA_MATERIAL_VALUATION,
        )
        return {}
    by_lower = {name.lower(): name for name in present}
    matnr, verpr = by_lower.get("matnr"), by_lower.get("verpr")
    if matnr is None or verpr is None:
        logger.warning("%s has no Matnr/Verpr columns; unit_price not filled", ODATA_MATERIAL_VALUATION)
        return {}
    peinh, waers = by_lower.get("peinh"), by_lower.get("waers")

    quote = session.get_bind().dialect.identifier_preparer.quote
    selected = {"m": matnr, "v": verpr, "p": peinh, "w": waers}
    statement = "SELECT " + ", ".join(
        f"{quote(column)} AS {alias}" if column else f"NULL AS {alias}" for alias, column in selected.items()
    ) + f" FROM {ODATA_MATERIAL_VALUATION}"

    prices: dict[str, tuple[Decimal, str | None]] = {}
    for row in _read(session, statement):
        material = odata_key(row.m)
        if material is None or clean(row.v) is None:
            continue
        price, per = parse_decimal(row.v), parse_decimal(row.p) if peinh else Decimal(1)
        if price is None or per is None or per <= 0:
            rejections.add(ODATA_MATERIAL_VALUATION, material, RejectionReason.INVALID_QUANTITY,
                           f"Verpr={row.v!r} Peinh={row.p!r}")
            continue
        _keep_highest(prices, material, price / per, clean(row.w))
    logger.info("materials: unit price and currency from %s (%d materials)", ODATA_MATERIAL_VALUATION, len(prices))
    return prices


def _materials_in_plant_scope(session: Session, scope: frozenset[str]) -> set[str] | None:
    """Materials present at one of the scoped plants, or ``None`` for no scope.

    ``None`` rather than "every material" so the caller can tell "no filter
    configured" from "the filter matched nothing" -- the second is a mistake
    worth seeing, and an empty set would silently stage zero materials.

    Read from n_marc and n_mard together: a material can hold stock at a plant
    with no MARC row, and dropping it would lose a real position.
    """
    if not scope:
        return None
    placeholders = ", ".join(f":p{i}" for i in range(len(scope)))
    params = {f"p{i}": plant for i, plant in enumerate(sorted(scope))}
    found: set[str] = set()
    for table in ("n_marc", "n_mard"):
        rows = _read(
            session,
            f"SELECT DISTINCT material FROM {table} WHERE plant IN ({placeholders})",
            params,
        )
        found.update(m for m in (clean(r.material) for r in rows) if m is not None)
    logger.info("plant scope %s: %d material(s) in scope", ",".join(sorted(scope)), len(found))
    return found


def _stage_materials(
    session: Session, run_id: int, policy: ExtractIngestionPolicy, rejections: _Rejections
) -> int:
    prices = _price_by_material(session, rejections)
    in_scope_materials = _materials_in_plant_scope(session, policy.plant_scope)

    def rows() -> Iterator[dict[str, Any]]:
        for row in _read(session, _material_sql(session)):
            material = clean(row.material)
            if material is None:
                rejections.add("n_mara", None, RejectionReason.MISSING_MATERIAL)
                continue
            # MARA is client-level and carries no plant, so a material is in
            # scope when it exists at one of the scoped plants. Staging the
            # rest would leave material rows no material-plant row ever
            # references -- harmless in isolation, but every catalogue-wide
            # percentage the reports quote is counted over this table.
            if in_scope_materials is not None and material not in in_scope_materials:
                continue
            yield {
                "sap_material_number": material,
                # Left null: no exposed SAP field maps to the app vocabulary.
                "app_material_id": None,
                "description": clean(row.material_description),
                "material_group": clean(row.material_group),
                "material_type": clean(row.material_type),
                "base_unit_of_measure": clean(row.base_unit_of_measure),
                "material_status": clean(row.x_plant_matl_status),
                "external_material_group": clean(row.ext_material_group),
                "manufacturer": clean(row.manufacturer),
                # Unlike the MARC-level flag below, this one is not exposed to
                # the LVORM "1" vs "X" bug: n_mara.df_at_client_level resolves
                # only against raw_mara (no odata_material_plant-style raw
                # overlay exists for MARA fields), so it is always the
                # workbook encoding parse_flag() expects. If an OData MARA
                # fallback is ever added, it will need the same
                # odata_flag_to_canonical() conversion this file's MARC path
                # now applies.
                "deletion_flag": parse_flag(row.df_at_client_level),
                "criticality": clean(row.criticality),
                "unit_price": prices.get(material, (None, None))[0],
                "currency": prices.get(material, (None, None))[1],
                "source_table": "n_mara",
                "staging_run_id": run_id,
            }

    staged = 0
    for batch in _batched(rows(), _safe_batch_size(StagedMaterial, policy.batch_size, session)):
        _upsert(session, StagedMaterial, batch, ["sap_material_number"])
        staged += len(batch)
    return staged


# --- Material-plant ----------------------------------------------------

# EISBE (safety stock) IS delivered in the MARC extract -- verified 2026-10-07
# against raw_marc in Azure SQL: the column is present and 37 rows carry a
# non-zero value. It was previously declared absent (field_map's
# MARC_MISSING_FIELDS) and staged as a hardcoded NULL, which discarded every
# one of those maintained values and made the "current vs recommended" safety
# stock comparison wrong for exactly the materials somebody had bothered to
# maintain. A blank or unreadable value still stages as NULL, never 0.
_MATERIAL_PLANT_SQL = """
    SELECT material, plant, mrp_type, mrp_controller, planned_deliv_time,
           reorder_point, safety_stock, maximum_stock_level, df_at_plant_level
      FROM n_marc
"""

# The MARC labels the live CSV cannot fill, and the OData MaterialPlantSet
# property that carries each. Used only for a label raw_marc does not have.
MRP_FIELDS_FROM_ODATA: dict[str, str] = {
    "mrp_type": "Dismm",
    "planned_deliv_time": "Plifz",
    "reorder_point": "Minbe",
    "maximum_stock_level": "Mabst",
    "df_at_plant_level": "Lvorm",
}
ODATA_MATERIAL_PLANT = "odata_material_plant"


def odata_key(value: object | None) -> str | None:
    """An OData key as the normalise views spell it: trimmed, leading zeros off.

    MaterialPlantSet pads MATNR to 18 characters; n_marc does not.
    """
    text_value = clean(value)
    return None if text_value is None else (text_value.lstrip("0") or None)


def odata_flag_to_canonical(value: object | None) -> str | None:
    """OData's ``Lvorm`` encoding ("1" for deleted) to the workbook's ("X").

    Decode by declared source encoding, never by guessing from the value:
    parse_flag() only recognises DELETION_FLAG_TRUE ("X"), which is correct
    for n_marc (workbook vocabulary) but wrong for a raw OData value passed
    through unconverted -- "1" != "X" silently reads every deleted
    material-plant as active. This is the one conversion step between the
    two; parse_flag() itself stays strict.

    Any value other than the OData true-encoding passes through unchanged
    (including blank, which both sources spell the same way), so a genuinely
    unexpected value is still visible downstream rather than laundered here.
    """
    text_value = clean(value)
    if text_value is None:
        return None
    return DELETION_FLAG_TRUE if text_value == ODATA_DELETION_FLAG_TRUE else text_value


def _columns(session: Session, table: str) -> list[str] | None:
    inspector = inspect(session.get_bind())
    real = {name.lower(): name for name in inspector.get_table_names()}.get(table.lower())
    return [c["name"] for c in inspector.get_columns(real)] if real else None


def _marc_gaps(session: Session) -> list[str]:
    """The MRP labels raw_marc cannot supply, decided from its columns once.

    The same resolution the normalise view used: a label with no source
    column in either vocabulary is a gap for the whole table, not for a row.
    """
    present = _columns(session, "raw_marc")
    if present is None:
        return list(MRP_FIELDS_FROM_ODATA)
    missing = {r.label for r in sap_normalise.resolve("marc", present) if r.source is None}
    return [label for label in MRP_FIELDS_FROM_ODATA if label in missing]


def _odata_mrp_fields(session: Session, labels: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """``(material, plant) -> {label: value}`` from odata_material_plant.

    Empty when there is nothing to fill or the table is not there; the gap then
    stays NULL and is logged, never defaulted.
    """
    if not labels:
        return {}
    present = _columns(session, ODATA_MATERIAL_PLANT)
    if present is None:
        logger.warning(
            "raw_marc has no %s and %s does not exist: those stage as NULL. "
            "Run: python -m app.ingest --fetch --load --set MaterialPlantSet",
            ", ".join(labels), ODATA_MATERIAL_PLANT,
        )
        return {}
    by_lower = {name.lower(): name for name in present}
    wanted = {label: by_lower.get(MRP_FIELDS_FROM_ODATA[label].lower()) for label in labels}
    matnr, werks = by_lower.get("matnr"), by_lower.get("werks")
    if matnr is None or werks is None:
        logger.warning("%s has no Matnr/Werks columns; MRP fields not filled", ODATA_MATERIAL_PLANT)
        return {}

    quote = session.get_bind().dialect.identifier_preparer.quote
    selected = [c for c in wanted.values() if c is not None]
    statement = (
        f"SELECT {quote(matnr)} AS m, {quote(werks)} AS w"
        + "".join(f", {quote(c)}" for c in selected)
        + f" FROM {ODATA_MATERIAL_PLANT}"
    )
    fields: dict[tuple[str, str], dict[str, Any]] = {}
    for row in session.execute(text(statement)).mappings():
        material, plant = odata_key(row["m"]), clean(row["w"])
        if material is None or plant is None:
            continue
        fields[(material, plant)] = {
            label: (row[column] if column is not None else None) for label, column in wanted.items()
        }
    logger.info(
        "material-plants: %s from %s (%d rows); raw_marc does not carry them",
        ", ".join(labels), ODATA_MATERIAL_PLANT, len(fields),
    )
    return fields


def _stage_material_plants(
    session: Session, run_id: int, policy: ExtractIngestionPolicy, rejections: _Rejections
) -> int:
    gaps = _marc_gaps(session)
    from_odata = _odata_mrp_fields(session, gaps)
    source_table = f"n_marc+{ODATA_MATERIAL_PLANT}" if from_odata else "n_marc"
    scope = policy.plant_scope
    skipped_out_of_scope = [0]

    def rows() -> Iterator[dict[str, Any]]:
        for row in _read(session, _MATERIAL_PLANT_SQL):
            material = clean(row.material)
            plant = clean(row.plant)
            if material is None:
                rejections.add("n_marc", None, RejectionReason.MISSING_MATERIAL)
                continue
            if plant is None:
                rejections.add("n_marc", material, RejectionReason.MISSING_PLANT)
                continue
            # Out of scope is not a rejection: the row is well-formed and
            # belongs to a site this initiative does not cover, which is a
            # business boundary rather than a data fault. Counting it as one
            # would bury the real rejections in thousands of entries.
            if _out_of_scope(plant, scope):
                skipped_out_of_scope[0] += 1
                continue
            values = dict(row._mapping)
            odata_values = from_odata.get((material, plant), {})
            # OData's df_at_plant_level (Lvorm) spells "deleted" as "1", not
            # the workbook's "X" -- convert before it reaches parse_flag(),
            # which only recognises the workbook encoding. Every other
            # OData-filled field here (DISMM/PLIFZ/MINBE/MABST) is a code or
            # number, not a boolean, so only this one needs translating.
            if "df_at_plant_level" in odata_values:
                odata_values = dict(odata_values)
                odata_values["df_at_plant_level"] = odata_flag_to_canonical(
                    odata_values["df_at_plant_level"]
                )
            values.update(odata_values)
            yield {
                "sap_material_number": material,
                "sap_plant_code": plant,
                "app_plant_id": None,
                # Staged as found. Blank stays blank: "not maintained" is a
                # real state, and the OAR policy -- not the adapter -- decides
                # what it means.
                "mrp_type": clean(values["mrp_type"]),
                # DISPO, upper-cased so the OAR predicate matches the way I13's
                # classify_material_scope does (trim + upper). Blank stays NULL.
                "mrp_controller": _upper(clean(values["mrp_controller"])),
                "planned_delivery_time_days": parse_int(values["planned_deliv_time"]),
                # Staged as delivered. parse_decimal returns None for a blank
                # or unparseable value, so "not supplied" stays NULL rather
                # than becoming 0 -- zero safety stock is a real and different
                # claim from "not maintained".
                "current_safety_stock": parse_decimal(values["safety_stock"]),
                "current_reorder_point": parse_decimal(values["reorder_point"]),
                "current_maximum_stock": parse_decimal(values["maximum_stock_level"]),
                "deletion_flag": parse_flag(values["df_at_plant_level"]),
                "source_table": source_table,
                "staging_run_id": run_id,
            }

    staged = 0
    for batch in _batched(rows(), _safe_batch_size(StagedMaterialPlant, policy.batch_size, session)):
        _upsert(session, StagedMaterialPlant, batch, ["sap_material_number", "sap_plant_code"])
        staged += len(batch)
    if skipped_out_of_scope[0]:
        logger.info(
            "plant scope %s: staged %d material-plant row(s), skipped %d outside it",
            ",".join(sorted(scope)), staged, skipped_out_of_scope[0],
        )
    return staged


# --- Stock (MARD) -------------------------------------------------------

# See MARD_FIELDS in field_map for the full mapping and why only these six
# stock columns are staged.
_STOCK_SQL = """
    SELECT material, plant, storage_location,
           unrestricted, stock_in_transfer, in_quality_insp,
           restricted_use_stock, blocked, returns
      FROM n_mard
"""


def _stage_stock(
    session: Session, run_id: int, policy: ExtractIngestionPolicy, rejections: _Rejections
) -> int:
    scope = policy.plant_scope

    def rows() -> Iterator[dict[str, Any]]:
        for row in _read(session, _STOCK_SQL):
            material = clean(row.material)
            plant = clean(row.plant)
            storage_location = clean(row.storage_location)

            if material is None:
                rejections.add("n_mard", None, RejectionReason.MISSING_MATERIAL)
                continue
            if plant is None:
                rejections.add("n_mard", material, RejectionReason.MISSING_PLANT)
                continue
            if _out_of_scope(plant, scope):
                continue
            if storage_location is None:
                # Not a documented rejection reason of its own: MARD's key
                # requires a storage location, so a missing one is the same
                # kind of gap as a missing plant.
                rejections.add("n_mard", material, RejectionReason.MISSING_PLANT)
                continue

            yield {
                "sap_material_number": material,
                "sap_plant_code": plant,
                "storage_location": storage_location,
                "unrestricted_use_stock": parse_decimal(row.unrestricted),
                "stock_in_transfer": parse_decimal(row.stock_in_transfer),
                "quality_inspection_stock": parse_decimal(row.in_quality_insp),
                "restricted_use_stock": parse_decimal(row.restricted_use_stock),
                "blocked_stock": parse_decimal(row.blocked),
                "returns_stock": parse_decimal(row.returns),
                "source_table": "n_mard",
                "staging_run_id": run_id,
            }

    staged = 0
    for batch in _batched(rows(), _safe_batch_size(StagedStock, policy.batch_size, session)):
        _upsert(
            session,
            StagedStock,
            batch,
            ["sap_material_number", "sap_plant_code", "storage_location"],
        )
        staged += len(batch)
    return staged


# --- Consumption -------------------------------------------------------

# The movement rows the policy counts, one per material document line. Totalled
# per material-plant-month by aggregate_consumption() below.
_CONSUMPTION_SELECT = """
    SELECT material, plant, posting_date, quantity, debit_credit_ind,
           movement_type, base_unit_of_measure
      FROM n_mseg
     WHERE movement_type IN :movement_types
"""

#: The unscoped statement: every plant. Kept as it was, and what an empty
#: ``I7_PLANTS`` runs.
_CONSUMPTION_SQL = text(_CONSUMPTION_SELECT).bindparams(
    bindparam("movement_types", expanding=True)
)


def _consumption_statement(scope: frozenset[str]) -> Any:
    """The movement query, narrowed to ``scope`` in SQL when there is one.

    Measured on the full extract: 1,160,647 movement rows across all plants,
    848,125 in the two scoped plants. The rest used to be fetched and thrown
    away in Python.
    """
    statement = text(_CONSUMPTION_SELECT + _plant_scope_clause("plant", scope, "material")).bindparams(
        bindparam("movement_types", expanding=True)
    )
    return _scoped(statement, scope)


def _consumption_totals(
    session: Session, policy: ExtractIngestionPolicy, rejections: _Rejections
) -> dict[tuple[str, str, date], dict[str, Any]]:
    """Monthly consumption per material-plant, aggregated from a stream.

    ``aggregate_consumption`` reads its rows once and keeps one entry per
    material-plant-month, so the million-odd movements are never held -- only
    the totals are, which is a small fraction of them. Unchanged by this: which
    rows count, how issues and reversals net, and which rows are rejected.
    """
    scope = policy.plant_scope
    params: dict[str, Any] = {"movement_types": list(policy.consumption.all_movement_types)}
    if scope:
        params["plant_scope"] = sorted(scope)
    return aggregate_consumption(
        _stream(session, _consumption_statement(scope), params),
        frozenset(policy.consumption.issue_movement_types),
        frozenset(policy.consumption.reversal_movement_types),
        rejections,
        scope,
    )


def aggregate_consumption(
    rows: Iterable[Any],
    issue_types: frozenset[str],
    reversal_types: frozenset[str],
    rejections: _Rejections,
    plant_scope: frozenset[str] = frozenset(),
) -> dict[tuple[str, str, date], dict[str, Any]]:
    """Net monthly consumption per material-plant, from movement rows.

    Issues count positive and reversals negative via SHKZG (credit ``H`` adds,
    anything else subtracts), so a cancelled issue nets out instead of
    inflating demand. issue_count/reversal_count split by movement TYPE, not by
    SHKZG -- the SOP 3.1.1 trigger needs a transaction-level event count, and
    movement type is what the ingestion policy's issue/reversal sets are
    defined over.

    A row with no material, no plant, an unparseable date or an unparseable
    quantity is rejected and counted, never silently dropped.
    """
    totals: dict[tuple[str, str, date], dict[str, Any]] = {}
    for row in rows:
        material, plant = clean(row.material), clean(row.plant)
        if material is None:
            rejections.add("n_mseg", None, RejectionReason.MISSING_MATERIAL)
            continue
        if plant is None:
            rejections.add("n_mseg", material, RejectionReason.MISSING_PLANT)
            continue
        if _out_of_scope(plant, plant_scope):
            continue
        posted = parse_date(row.posting_date)
        if posted is None:
            rejections.add("n_mseg", f"{material}/{plant}", RejectionReason.INVALID_DATE,
                           str(row.posting_date or "")[:40])
            continue
        quantity = parse_decimal(row.quantity)
        if quantity is None:
            rejections.add("n_mseg", f"{material}/{plant}", RejectionReason.INVALID_QUANTITY,
                           str(row.quantity or "")[:40])
            continue

        key = (material, plant, first_of_month(posted))
        entry = totals.setdefault(
            key,
            {"quantity": Decimal(0), "unit_of_measure": None, "movement_count": 0,
             "issue_count": 0, "reversal_count": 0},
        )
        entry["quantity"] += quantity if clean(row.debit_credit_ind) == CREDIT_INDICATOR else -quantity
        entry["movement_count"] += 1
        movement = clean(row.movement_type)
        if movement in issue_types:
            entry["issue_count"] += 1
        if movement in reversal_types:
            entry["reversal_count"] += 1
        unit = clean(row.base_unit_of_measure)
        if unit is not None and (entry["unit_of_measure"] is None or unit > entry["unit_of_measure"]):
            entry["unit_of_measure"] = unit
    return totals


def _stage_consumption(
    session: Session, run_id: int, policy: ExtractIngestionPolicy, rejections: _Rejections
) -> int:
    # Fully consumed here, before the first upsert below: the stream is exhausted
    # (and its result closed) when aggregate_consumption returns.
    totals = _consumption_totals(session, policy, rejections)

    def rows() -> Iterator[dict[str, Any]]:
        for (material, plant, period), entry in totals.items():
            yield {
                "sap_material_number": material,
                "sap_plant_code": plant,
                "period": period,
                **entry,
                "source_table": "n_mseg",
                "staging_run_id": run_id,
            }

    staged = 0
    for batch in _batched(rows(), _safe_batch_size(StagedConsumption, policy.batch_size, session)):
        _upsert(
            session,
            StagedConsumption,
            batch,
            ["sap_material_number", "sap_plant_code", "period"],
        )
        staged += len(batch)
    return staged


# --- Purchase orders ---------------------------------------------------

# One row per PO line with its header. The earliest goods receipt and the
# earliest schedule date are folded in from receipts_by_line() and
# schedule_dates_by_line(): a PO line can have several of each, and folding
# them in Python keeps the join 1:1 without text-typed MIN/SUM in SQL.
_PURCHASE_ORDER_SQL = """
    SELECT p.purchasing_document,
           p.item,
           p.material,
           p.plant,
           p.order_quantity,
           p.planned_deliv_time,
           p.deletion_indicator AS item_deletion,
           k.created_on,
           k.supplier,
           k.deletion_indicator AS header_deletion
      FROM n_ekpo p
      LEFT JOIN n_ekko k ON k.purchasing_document = p.purchasing_document
     WHERE NULLIF(p.material, '') IS NOT NULL
"""


def _purchase_order_statement(scope: frozenset[str]) -> Any:
    """The PO-line query, narrowed to ``scope`` in SQL when there is one.

    Measured on the full extract: 700,922 lines across all plants, 472,321 in
    the two scoped plants. Unlike the movement read this one cannot be streamed
    -- its rows are upserted while they are produced, and SQL Server refuses a
    MERGE with a result open -- so narrowing it is what keeps it small.
    """
    return _scoped(
        text(
            _PURCHASE_ORDER_SQL
            + _plant_scope_clause("p.plant", scope, "p.purchasing_document", "p.item")
        ),
        scope,
    )

_GOODS_RECEIPT_SQL = """
    SELECT purchasing_document, item, posting_date, quantity
      FROM n_ekbe
     WHERE po_history_category = :gr_category
       AND movement_type = :gr_movement
"""

_SCHEDULE_SQL = "SELECT purchasing_document, item, delivery_date FROM n_eket"


def receipts_by_line(
    rows: Iterable[Any], rejections: _Rejections
) -> dict[tuple[str, str], tuple[date, Decimal]]:
    """``(document, item) -> (earliest GR date, total received)``.

    A receipt with an unparseable date or quantity is rejected and left out of
    both figures, as the SQL this replaced excluded it -- but counted now.
    """
    receipts: dict[tuple[str, str], tuple[date, Decimal]] = {}
    for row in rows:
        document, item = clean(row.purchasing_document), clean(row.item)
        if document is None or item is None:
            continue
        posted, quantity = parse_date(row.posting_date), parse_decimal(row.quantity)
        if posted is None:
            rejections.add("n_ekbe", f"{document}/{item}", RejectionReason.INVALID_DATE)
            continue
        if quantity is None:
            rejections.add("n_ekbe", f"{document}/{item}", RejectionReason.INVALID_QUANTITY)
            continue
        earliest, total = receipts.get((document, item), (posted, Decimal(0)))
        receipts[(document, item)] = (min(earliest, posted), total + quantity)
    return receipts


def schedule_dates_by_line(rows: Iterable[Any]) -> dict[tuple[str, str], date]:
    """``(document, item) -> earliest schedule-line delivery date``."""
    dates: dict[tuple[str, str], date] = {}
    for row in rows:
        document, item = clean(row.purchasing_document), clean(row.item)
        delivery = parse_date(row.delivery_date)
        if document is None or item is None or delivery is None:
            continue
        key = (document, item)
        if key not in dates or delivery < dates[key]:
            dates[key] = delivery
    return dates


def _stage_purchase_orders(
    session: Session, run_id: int, policy: ExtractIngestionPolicy, rejections: _Rejections
) -> int:
    # Goods receipts (1.4M rows) and schedule lines (0.7M) are streamed into the
    # dictionaries that fold them per PO line; each is exhausted before the next
    # statement runs, and all of it before the first upsert.
    receipts = receipts_by_line(
        _stream(
            session,
            _GOODS_RECEIPT_SQL,
            {"gr_category": GOODS_RECEIPT_HISTORY_CATEGORY, "gr_movement": GOODS_RECEIPT_MOVEMENT_TYPE},
        ),
        rejections,
    )
    schedule = schedule_dates_by_line(_stream(session, _SCHEDULE_SQL))
    scope = policy.plant_scope
    po_params: dict[str, Any] = {"plant_scope": sorted(scope)} if scope else {}

    def rows() -> Iterator[dict[str, Any]]:
        for row in _read(session, _purchase_order_statement(scope), po_params):
            document = clean(row.purchasing_document)
            item = clean(row.item)
            material = clean(row.material)
            plant = clean(row.plant)

            if document is None or item is None:
                rejections.add("n_ekpo", material, RejectionReason.MISSING_PURCHASING_DOCUMENT)
                continue
            key = f"{document}/{item}"
            if material is None:
                rejections.add("n_ekpo", key, RejectionReason.MISSING_MATERIAL)
                continue
            if plant is None:
                rejections.add("n_ekpo", key, RejectionReason.MISSING_PLANT)
                continue
            if _out_of_scope(plant, scope):
                continue

            created_on = parse_date(row.created_on)
            goods_receipt, quantity_received = receipts.get((document, item), (None, None))

            lead_time = None
            if created_on is not None and goods_receipt is not None:
                delta = (goods_receipt - created_on).days
                if delta < 0:
                    # Impossible, so the pair is untrustworthy. The line still
                    # stages -- it is a real PO -- but with no lead time.
                    rejections.add(
                        "n_ekpo",
                        key,
                        RejectionReason.RECEIPT_BEFORE_CREATION,
                        f"GR {goods_receipt} precedes PO {created_on}",
                    )
                else:
                    # Stored unfiltered: the 1-730 day window is Phase 3 policy.
                    lead_time = delta

            # LOEKZ is a code (L deleted, S blocked), not an X boolean.
            # Either level marks the line dead -- a deleted header kills its items.
            cancelled = parse_purchasing_deletion(row.item_deletion) or parse_purchasing_deletion(
                row.header_deletion
            )

            yield {
                "purchasing_document": document,
                "item": item,
                "sap_material_number": material,
                "sap_plant_code": plant,
                "created_on": created_on,
                "goods_receipt_date": goods_receipt,
                "planned_delivery_date": schedule.get((document, item)),
                "planned_delivery_time_days": parse_int(row.planned_deliv_time),
                "quantity_ordered": parse_decimal(row.order_quantity),
                "quantity_received": quantity_received,
                "supplier": clean(row.supplier),
                "is_cancelled": bool(cancelled),
                "lead_time_days": lead_time,
                "source_table": "n_ekpo",
                "staging_run_id": run_id,
            }

    staged = 0
    for batch in _batched(rows(), _safe_batch_size(StagedPurchaseOrder, policy.batch_size, session)):
        _upsert(session, StagedPurchaseOrder, batch, ["purchasing_document", "item"])
        staged += len(batch)
    return staged


# --- Deactivation ------------------------------------------------------


# The tables the feature universe is built from, and the only ones swept.
# Consumption, purchase orders and material attributes are deliberately NOT
# swept: they are historical records of events that happened. A movement from
# March is not retracted by its absence from a June extract -- the extract
# simply does not reach back that far -- and deactivating it would rewrite
# demand history every time the extract window moved forward.
_SWEPT = (StagedMaterialPlant, StagedStock)


def deactivate_unseen(session: Session, staging_run_id: int) -> int:
    """Reconcile ``is_active`` against what this run staged. Returns the number
    of rows DEACTIVATED (reactivations are logged, not counted, because the
    figure stored on the run row answers "what did this extract drop?").

    **Only ever called for a run over a complete snapshot** -- the caller gates
    this, and ``stage_extract`` records the gate it applied on the run row.
    Absence from a delta means "unchanged", and sweeping on one would deactivate
    everything that merely did not move.

    The test is ``staging_run_id``: the upsert refreshes that column on every
    row it writes, so a row re-seen carries this run's id and a row that has
    vanished keeps an older one.

    **Both directions are written explicitly.** Reactivation cannot be left to
    the upsert: ``app.core.upsert._updatable`` builds its UPDATE column list
    from the keys actually present in the staged dictionaries, and those do not
    carry ``is_active`` -- the column exists for this sweep to own, not for the
    adapter to supply per row. So a row deactivated in June and present again in
    July is re-staged with a fresh ``staging_run_id`` but keeps
    ``is_active = 0``, and without the second statement below it would stay out
    of scope permanently while looking perfectly current.
    """
    total = 0
    for model in _SWEPT:
        gone = session.execute(
            update(model)
            .where(model.staging_run_id != staging_run_id, model.is_active.is_(True))
            .values(is_active=False)
        )
        total += gone.rowcount or 0

        back = session.execute(
            update(model)
            .where(model.staging_run_id == staging_run_id, model.is_active.is_(False))
            .values(is_active=True)
        )
        if back.rowcount:
            logger.info(
                "staging run %d: %d %s row(s) reactivated -- present again in "
                "this snapshot",
                staging_run_id,
                back.rowcount,
                model.__tablename__,
            )
    return total


# --- Entry point -------------------------------------------------------


#: The staging steps, in order: (label, StagingResult attribute, StagingRun
#: column, function). Independent of each other -- each reads the raw layer on
#: its own -- which is what lets a resumed run skip the ones already saved.
_STEPS: tuple[tuple[str, str, str, Any], ...] = (
    ("materials", "materials", "materials_staged", _stage_materials),
    ("material-plants", "material_plants", "material_plants_staged", _stage_material_plants),
    ("stock", "stock", "stock_staged", _stage_stock),
    ("consumption", "consumption", "consumption_staged", _stage_consumption),
    ("purchase orders", "purchase_orders", "purchase_orders_staged", _stage_purchase_orders),
)

#: Held for the life of a staging run (``sp_getapplock``, session-owned, so SQL
#: Server lets go if the process dies). Holding it proves no other staging run
#: is alive, which is what makes it safe to judge a ``running`` row dead.
STAGING_LOCK = "spares_ai_i7_staging"

STATUS_RUNNING = "running"

ABANDONED = (
    "abandoned: its process stopped (an App Service restart, a deploy or an OOM) "
    "before the run finished; found by staging run {by}"
)


class _NoLock:
    """Stand-in where there is no SQL Server to hold an application lock
    (the SQLite tests). Every real deployment is Azure SQL."""

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        return None


def _default_lock() -> Any:
    from app.core.db import get_engine

    if get_engine().dialect.name != "mssql":
        return _NoLock()
    from app.ingest.scheduler import _Lock

    return _Lock(STAGING_LOCK)


def _current_fingerprint(session: Session) -> str | None:
    from app.initiatives.i7.watch import source_fingerprint

    try:
        return source_fingerprint(session) or None
    except Exception:  # noqa: BLE001 -- lineage only; never fail staging over it
        session.rollback()
        return None


def _claim_run(
    session: Session,
    *,
    movement_types: str,
    snapshot_complete: bool,
    source_fingerprint: str | None,
    resume: bool,
) -> tuple[StagingRun, bool]:
    """The run to write into, and whether it is a resumed one. The caller
    holds the staging lock and commits.

    **Resumed** when the newest run did not succeed, saved at least one step,
    and read the same thing this run would: the same source fingerprint (both
    known), the same consumption movement types and the same snapshot
    completeness. Anything else starts a new run -- resuming across a reload of
    the raw layer would mix two versions of the data in one run.

    Every other run still marked ``running`` is dead (the lock proves it) and
    is marked ``failed``, so it stops blocking the feature build, which refuses
    to start while any staging run says ``running``.
    """
    newest = session.execute(select(StagingRun).order_by(StagingRun.id.desc()).limit(1)).scalars().first()
    candidate = None
    if (
        resume
        and newest is not None
        and newest.status != STATUS_SUCCEEDED
        and any(getattr(newest, column) for _, _, column, _ in _STEPS)
        and source_fingerprint is not None
        and newest.source_fingerprint == source_fingerprint
        and (newest.consumption_movement_types or "") == movement_types
        and bool(newest.snapshot_complete) == snapshot_complete
    ):
        candidate = newest

    if candidate is not None:
        run = candidate
        run.status = STATUS_RUNNING
        run.error = None
        run.finished_at = None
        session.flush()
    else:
        run = StagingRun(
            source=SOURCE_NORMALISE_VIEWS,
            status=STATUS_RUNNING,
            consumption_movement_types=movement_types,
            snapshot_complete=snapshot_complete,
            source_fingerprint=source_fingerprint,
        )
        session.add(run)
        session.flush()

    stale = session.execute(
        select(StagingRun).where(StagingRun.status == STATUS_RUNNING, StagingRun.id != run.id)
    ).scalars().all()
    now = datetime.now(timezone.utc)
    for old in stale:
        old.status = STATUS_FAILED
        old.error = ABANDONED.format(by=run.id)
        old.finished_at = now
    if stale:
        logger.warning(
            "staging run %d: marked %d dead run(s) failed: %s",
            run.id, len(stale), ", ".join(str(old.id) for old in stale),
        )
    return run, candidate is not None


def stage_extract(
    policy: ExtractIngestionPolicy | None = None,
    *,
    snapshot_complete: bool = False,
    source_fingerprint: str | None = None,
    resume: bool = True,
    lock: Any = None,
) -> StagingResult:
    """Stage the SAP raw layer, through the normalise views, into canonical staging.

    Idempotent: running twice converges on the same state. Raw tables are only
    ever read.

    **Each step is committed as it finishes**, together with its count on the
    run row. Staging the full extract takes about an hour on the nonprod
    database, and it used to be one transaction: every restart in that hour (a
    deploy, a manual restart, Azure moving the app to another machine) rolled
    the whole hour back -- nine runs in a row on 12-13 Oct 2026. Now a restart
    costs the step in progress, and the next call **resumes** the interrupted
    run from the first step not yet saved (``resume=False`` forces a fresh
    run; see :func:`_claim_run` for when a run is resumable).

    The run stays ``running`` until every step is saved, and the feature build
    refuses to read a run in that state, so a partly staged run is never
    built from.

    ``policy`` defaults to one carrying ``I7_PLANTS`` (app.core.config), so a
    normal run is scoped to the plants VZI named without every caller having
    to know they exist. A caller passing its own policy decides for itself --
    ``ExtractIngestionPolicy()`` alone still means every plant, which is what
    the tests construct and what this did before plant scope existed.

    ``snapshot_complete`` says whether the raw layer this run reads is a
    complete snapshot of its sources rather than a delta merged into one. It
    gates the deactivation sweep and nothing else, and it **defaults False**:
    a caller that has not established the question's answer must not sweep.
    Only the daily CSV full refresh replaces ``raw_<table>`` whole
    (``app.ingest.csv_load``), so only a run following one may pass True. The
    sweep runs only after every step is saved, so a run that stops part-way
    never sweeps.

    ``source_fingerprint`` records which raw-layer load was read, for lineage;
    when not given it is read from ``ingestion_run``, which is also what makes
    a resume safe to judge.
    """
    if policy is None:
        from app.core.config import get_settings

        policy = ExtractIngestionPolicy(plants=get_settings().i7_plant_list)
    lock = lock if lock is not None else _default_lock()
    if not lock.acquire():
        return StagingResult(
            status=STATUS_FAILED,
            error="another staging run holds the staging lock; it is already staging this data",
        )

    try:
        return _stage_locked(policy, snapshot_complete, source_fingerprint, resume)
    finally:
        lock.release()


def _stage_locked(
    policy: ExtractIngestionPolicy,
    snapshot_complete: bool,
    source_fingerprint: str | None,
    resume: bool,
) -> StagingResult:
    session_factory = get_sessionmaker()

    with session_factory() as session:
        fingerprint = source_fingerprint or _current_fingerprint(session)
        run, resumed = _claim_run(
            session,
            movement_types=",".join(policy.consumption.all_movement_types),
            snapshot_complete=snapshot_complete,
            source_fingerprint=fingerprint,
            resume=resume,
        )
        session.commit()
        run_id = run.id
        saved = {column: getattr(run, column) or 0 for _, _, column, _ in _STEPS}
        rejected_before = run.rejected or 0

    result = StagingResult(run_id=run_id)
    rejections = _Rejections(run_id)
    if resumed:
        logger.info(
            "staging run %d: resuming -- already saved: %s",
            run_id,
            ", ".join(f"{label} {saved[column]:,}" for label, _, column, _ in _STEPS if saved[column]),
        )

    try:
        with session_factory() as session:
            for label, attribute, column, step in _STEPS:
                if saved[column]:
                    setattr(result, attribute, saved[column])
                    logger.info("staging run %d: %s already saved (%d rows), skipped", run_id, label, saved[column])
                    continue
                logger.info("staging run %d: %s", run_id, label)
                count = step(session, run_id, policy, rejections)
                setattr(result, attribute, count)
                rejections.flush(session)
                stored = session.get(StagingRun, run_id)
                setattr(stored, column, count)
                stored.rejected = rejected_before + sum(rejections.counts.values())
                # The step's rows and its count in one commit: a count on the
                # run row means the step is saved, which is what resume reads.
                session.commit()
                logger.info("staging run %d: %s saved (%d rows)", run_id, label, count)

            result.rejections = dict(rejections.counts)

            if snapshot_complete:
                result.deactivated = deactivate_unseen(session, run_id)
                logger.info(
                    "staging run %d: %d row(s) deactivated -- absent from a "
                    "complete snapshot",
                    run_id,
                    result.deactivated,
                )
            else:
                logger.info(
                    "staging run %d: no deactivation sweep (not a complete "
                    "snapshot); absence here means unchanged, not deleted",
                    run_id,
                )

            stored = session.get(StagingRun, run_id)
            stored.status = STATUS_SUCCEEDED
            stored.deactivated = result.deactivated
            stored.rejected = rejected_before + result.rejected_total
            stored.finished_at = datetime.now(timezone.utc)
            session.commit()

        logger.info(
            "staging run %d: %d materials, %d material-plants, %d stock rows, "
            "%d consumption, %d purchase orders, %d rejected",
            run_id,
            result.materials,
            result.material_plants,
            result.stock,
            result.consumption,
            result.purchase_orders,
            rejected_before + result.rejected_total,
        )
        return result

    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        logger.error("staging run %d failed: %s", run_id, detail)
        result.status = STATUS_FAILED
        result.error = detail
        try:
            with session_factory() as session:
                stored = session.get(StagingRun, run_id)
                stored.status = STATUS_FAILED
                stored.error = detail[:4000]
                stored.finished_at = datetime.now(timezone.utc)
                session.commit()
        except Exception:
            logger.exception("staging run %d: could not record the failure either", run_id)
        return result

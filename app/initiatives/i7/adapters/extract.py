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
a query is fetched whole (:func:`_read`) and only then upserted -- interleaving a
streamed read with batched MERGEs worked on Postgres and failed on the first
batch on Azure SQL. The volumes are thousands to tens of thousands of rows.

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

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.core.upsert import safe_batch_size, upsert
from app.initiatives.i7.adapters.field_map import (
    CREDIT_INDICATOR,
    GOODS_RECEIPT_HISTORY_CATEGORY,
    GOODS_RECEIPT_MOVEMENT_TYPE,
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


def _read(session: Session, statement: str, params: dict[str, Any] | None = None) -> list[Any]:
    """Run a query and fetch every row, closing its result before returning.

    Fetched whole on purpose, not streamed: the caller upserts on the same
    connection, and SQL Server will not run a MERGE while a result is still
    open on it. See the module docstring.
    """
    return session.execute(text(statement), params or {}).all()


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
# ZMM065 is joined from its raw tables: it is a report only the workbook loader
# fills, always in the workbook vocabulary, with material numbers unpadded --
# the same form the normalise views give n_mara.material.
_MATERIAL_SQL = """
    SELECT a.material,
           a.material_group,
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
            SELECT mat_code, MAX(criticality) AS criticality
              FROM (SELECT mat_code, criticality FROM raw_zmm065_bmm
                    UNION ALL
                    SELECT mat_code, criticality FROM raw_zmm065_gb) u
             WHERE NULLIF(criticality, '') IS NOT NULL
             GROUP BY mat_code
      ) z ON z.mat_code = a.material
"""
# MBEW carries no currency column in this extract, so unit_price is staged
# without one. Left NULL rather than assumed: the operating currency is ZAR by
# context, but writing that in would be inventing source data.

_PRICE_SQL = "SELECT material, moving_price FROM n_mbew"


def _price_by_material(session: Session, rejections: _Rejections) -> dict[str, Decimal]:
    """Highest moving price per material across valuation areas.

    Parsed before comparing: a MAX over the text column would rank "9.5" above
    "10.0". The live CSV MBEW carries no VERPR, so there every price is NULL and
    this is empty -- unit_price stages as NULL, a visible gap.
    """
    prices: dict[str, Decimal] = {}
    for row in _read(session, _PRICE_SQL):
        material = clean(row.material)
        if material is None or clean(row.moving_price) is None:
            continue
        price = parse_decimal(row.moving_price)
        if price is None:
            rejections.add("n_mbew", material, RejectionReason.INVALID_QUANTITY, "moving price")
            continue
        if material not in prices or price > prices[material]:
            prices[material] = price
    return prices


def _stage_materials(
    session: Session, run_id: int, policy: ExtractIngestionPolicy, rejections: _Rejections
) -> int:
    prices = _price_by_material(session, rejections)

    def rows() -> Iterator[dict[str, Any]]:
        for row in _read(session, _MATERIAL_SQL):
            material = clean(row.material)
            if material is None:
                rejections.add("n_mara", None, RejectionReason.MISSING_MATERIAL)
                continue
            yield {
                "sap_material_number": material,
                # Left null: no exposed SAP field maps to the app vocabulary.
                "app_material_id": None,
                "description": clean(row.material_description),
                "material_group": clean(row.material_group),
                "base_unit_of_measure": clean(row.base_unit_of_measure),
                "material_status": clean(row.x_plant_matl_status),
                "external_material_group": clean(row.ext_material_group),
                "manufacturer": clean(row.manufacturer),
                "deletion_flag": parse_flag(row.df_at_client_level),
                "criticality": clean(row.criticality),
                "unit_price": prices.get(material),
                "currency": None,
                "source_table": "n_mara",
                "staging_run_id": run_id,
            }

    staged = 0
    for batch in _batched(rows(), _safe_batch_size(StagedMaterial, policy.batch_size, session)):
        _upsert(session, StagedMaterial, batch, ["sap_material_number"])
        staged += len(batch)
    return staged


# --- Material-plant ----------------------------------------------------

# EISBE (safety stock) is absent from the MARC extract -- see MARC_MISSING_FIELDS
# in field_map. current_safety_stock therefore stages as NULL throughout.
_MATERIAL_PLANT_SQL = """
    SELECT material, plant, mrp_type, planned_deliv_time,
           reorder_point, maximum_stock_level, df_at_plant_level
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
            values = dict(row._mapping)
            values.update(from_odata.get((material, plant), {}))
            yield {
                "sap_material_number": material,
                "sap_plant_code": plant,
                "app_plant_id": None,
                # Staged as found. Blank stays blank: "not maintained" is a
                # real state, and the OAR policy -- not the adapter -- decides
                # what it means.
                "mrp_type": clean(values["mrp_type"]),
                "planned_delivery_time_days": parse_int(values["planned_deliv_time"]),
                # EISBE is not in the extract. NULL, not 0: zero safety stock is
                # a real and different claim from "not supplied".
                "current_safety_stock": None,
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
_CONSUMPTION_SQL = text(
    """
    SELECT material, plant, posting_date, quantity, debit_credit_ind,
           movement_type, base_unit_of_measure
      FROM n_mseg
     WHERE movement_type IN :movement_types
    """
).bindparams(bindparam("movement_types", expanding=True))


def aggregate_consumption(
    rows: Iterable[Any],
    issue_types: frozenset[str],
    reversal_types: frozenset[str],
    rejections: _Rejections,
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
    movement_rows = session.execute(
        _CONSUMPTION_SQL, {"movement_types": list(policy.consumption.all_movement_types)}
    ).all()
    totals = aggregate_consumption(
        movement_rows,
        frozenset(policy.consumption.issue_movement_types),
        frozenset(policy.consumption.reversal_movement_types),
        rejections,
    )

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
    receipts = receipts_by_line(
        _read(
            session,
            _GOODS_RECEIPT_SQL,
            {"gr_category": GOODS_RECEIPT_HISTORY_CATEGORY, "gr_movement": GOODS_RECEIPT_MOVEMENT_TYPE},
        ),
        rejections,
    )
    schedule = schedule_dates_by_line(_read(session, _SCHEDULE_SQL))

    def rows() -> Iterator[dict[str, Any]]:
        for row in _read(session, _PURCHASE_ORDER_SQL):
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


# --- Entry point -------------------------------------------------------


def stage_extract(policy: ExtractIngestionPolicy | None = None) -> StagingResult:
    """Stage the SAP raw layer, through the normalise views, into canonical staging.

    Idempotent: running twice converges on the same state. Raw tables are only
    ever read.
    """
    policy = policy or ExtractIngestionPolicy()
    session_factory = get_sessionmaker()

    with session_factory() as session:
        run = StagingRun(
            source=SOURCE_NORMALISE_VIEWS,
            status="running",
            consumption_movement_types=",".join(policy.consumption.all_movement_types),
        )
        session.add(run)
        session.commit()
        run_id = run.id

    result = StagingResult(run_id=run_id)
    rejections = _Rejections(run_id)

    try:
        with session_factory() as session:
            logger.info("staging run %d: materials", run_id)
            result.materials = _stage_materials(session, run_id, policy, rejections)

            logger.info("staging run %d: material-plants", run_id)
            result.material_plants = _stage_material_plants(session, run_id, policy, rejections)

            logger.info("staging run %d: stock", run_id)
            result.stock = _stage_stock(session, run_id, policy, rejections)

            logger.info("staging run %d: consumption", run_id)
            result.consumption = _stage_consumption(session, run_id, policy, rejections)

            logger.info("staging run %d: purchase orders", run_id)
            result.purchase_orders = _stage_purchase_orders(session, run_id, policy, rejections)

            rejections.flush(session)
            result.rejections = dict(rejections.counts)
            session.commit()

        with session_factory() as session:
            stored = session.get(StagingRun, run_id)
            stored.status = STATUS_SUCCEEDED
            stored.materials_staged = result.materials
            stored.material_plants_staged = result.material_plants
            stored.stock_staged = result.stock
            stored.consumption_staged = result.consumption
            stored.purchase_orders_staged = result.purchase_orders
            stored.rejected = result.rejected_total
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
            result.rejected_total,
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

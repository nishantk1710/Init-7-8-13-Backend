"""July/August extract -> canonical staging.

The only module in I07 that reads ``raw_*`` tables. Everything downstream reads
the staging tables or the canonical contracts built from them, so Phase 12
replaces this file rather than editing the pipeline.

**Aggregation happens in SQL, not Python.** Monthly consumption is a
``GROUP BY`` over 233k movement rows; pulling them into Python to total them
would move a lot of data to no purpose. Row-level work streams with
``yield_per`` and inserts in batches, so memory stays flat as the extract grows.

**Idempotency is a database property.** Each staging table has a unique natural
key, and writes go through ``ON CONFLICT DO UPDATE`` -- so a second run converges
on the same state instead of appending duplicates, and two concurrent runs cannot
interleave into one. This is the single documented exception to the repository's
"no dialect-specific SQL" rule; see :func:`_upsert`.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.initiatives.i7.adapters.field_map import (
    CREDIT_INDICATOR,
    GOODS_RECEIPT_HISTORY_CATEGORY,
    GOODS_RECEIPT_MOVEMENT_TYPE,
)
from app.initiatives.i7.adapters.ingestion_policy import ExtractIngestionPolicy
from app.initiatives.i7.adapters.validation import (
    RejectionReason,
    clean,
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

logger = logging.getLogger(__name__)

SOURCE_JULY_EXTRACT = "july_extract"
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


def _stream(session: Session, statement: str, params: dict[str, Any] | None = None) -> Iterator[Any]:
    """Stream a query server-side rather than buffering the whole result."""
    result = session.execute(text(statement), params or {}).yield_per(2000)
    yield from result


POSTGRES_MAX_PARAMETERS = 65535
"""Hard protocol limit on bind parameters in one statement.

A multi-row INSERT binds ``rows x columns`` parameters, so a batch size that is
safe for a 6-column table overflows a 14-column one. :func:`_safe_batch_size`
derives the real limit instead of hoping the configured batch fits.
"""


def _safe_batch_size(model: type, requested: int) -> int:
    """Largest batch that stays under the parameter ceiling."""
    columns = len(model.__table__.columns)
    return max(1, min(requested, POSTGRES_MAX_PARAMETERS // columns))


def _upsert(session: Session, model: type, rows: list[dict[str, Any]], conflict: list[str]) -> None:
    """Insert a batch, updating on natural-key conflict.

    ``ON CONFLICT`` is Postgres-specific, which the repository otherwise avoids
    for portability. It is used here deliberately: idempotency has to be
    enforced by the database, because an application-level existence check
    races with a concurrent run and silently produces duplicates. On a different
    engine this is the one function to port -- SQL Server's ``MERGE`` is the
    equivalent -- and the rest of the adapter is unaffected.
    """
    if not rows:
        return
    statement = postgres_insert(model).values(rows)
    updatable = {
        column.name: statement.excluded[column.name]
        for column in model.__table__.columns
        if column.name not in conflict and column.name != "id"
    }
    session.execute(statement.on_conflict_do_update(index_elements=conflict, set_=updatable))


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

_MATERIAL_SQL = """
    SELECT a.material,
           a.material_group,
           a.base_unit_of_measure,
           a.x_plant_matl_status,
           a.ext_material_group,
           a.manufacturer,
           a.df_at_client_level,
           t.material_description,
           z.criticality,
           b.moving_price
      FROM raw_mara a
      LEFT JOIN raw_makt t ON t.material = a.material
      LEFT JOIN (
            SELECT mat_code, MAX(criticality) AS criticality
              FROM (SELECT mat_code, criticality FROM raw_zmm065_bmm
                    UNION ALL
                    SELECT mat_code, criticality FROM raw_zmm065_gb) u
             WHERE NULLIF(criticality, '') IS NOT NULL
             GROUP BY mat_code
      ) z ON z.mat_code = a.material
      LEFT JOIN (
            SELECT material, MAX(moving_price) AS moving_price
              FROM raw_mbew GROUP BY material
      ) b ON b.material = a.material
"""
# MBEW carries no currency column in this extract, so unit_price is staged
# without one. Left NULL rather than assumed: the operating currency is ZAR by
# context, but writing that in would be inventing source data.


def _stage_materials(
    session: Session, run_id: int, policy: ExtractIngestionPolicy, rejections: _Rejections
) -> int:
    def rows() -> Iterator[dict[str, Any]]:
        for row in _stream(session, _MATERIAL_SQL):
            material = clean(row.material)
            if material is None:
                rejections.add("raw_mara", None, RejectionReason.MISSING_MATERIAL)
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
                "unit_price": parse_decimal(row.moving_price),
                "currency": None,
                "source_table": "raw_mara",
                "staging_run_id": run_id,
            }

    staged = 0
    for batch in _batched(rows(), _safe_batch_size(StagedMaterial, policy.batch_size)):
        _upsert(session, StagedMaterial, batch, ["sap_material_number"])
        staged += len(batch)
    return staged


# --- Material-plant ----------------------------------------------------

# EISBE (safety stock) is absent from the MARC extract -- see MARC_MISSING_FIELDS
# in field_map. current_safety_stock therefore stages as NULL throughout.
_MATERIAL_PLANT_SQL = """
    SELECT material, plant, mrp_type, planned_deliv_time,
           reorder_point, maximum_stock_level, df_at_plant_level
      FROM raw_marc
"""


def _stage_material_plants(
    session: Session, run_id: int, policy: ExtractIngestionPolicy, rejections: _Rejections
) -> int:
    def rows() -> Iterator[dict[str, Any]]:
        for row in _stream(session, _MATERIAL_PLANT_SQL):
            material = clean(row.material)
            plant = clean(row.plant)
            if material is None:
                rejections.add("raw_marc", None, RejectionReason.MISSING_MATERIAL)
                continue
            if plant is None:
                rejections.add("raw_marc", material, RejectionReason.MISSING_PLANT)
                continue
            yield {
                "sap_material_number": material,
                "sap_plant_code": plant,
                "app_plant_id": None,
                # Staged as found. Blank stays blank: "not maintained" is a
                # real state, and the OAR policy -- not the adapter -- decides
                # what it means.
                "mrp_type": clean(row.mrp_type),
                "planned_delivery_time_days": parse_int(row.planned_deliv_time),
                # EISBE is not in the extract. NULL, not 0: zero safety stock is
                # a real and different claim from "not supplied".
                "current_safety_stock": None,
                "current_reorder_point": parse_decimal(row.reorder_point),
                "current_maximum_stock": parse_decimal(row.maximum_stock_level),
                "deletion_flag": parse_flag(row.df_at_plant_level),
                "source_table": "raw_marc",
                "staging_run_id": run_id,
            }

    staged = 0
    for batch in _batched(rows(), _safe_batch_size(StagedMaterialPlant, policy.batch_size)):
        _upsert(session, StagedMaterialPlant, batch, ["sap_material_number", "sap_plant_code"])
        staged += len(batch)
    return staged


# --- Stock (MARD) -------------------------------------------------------

# The extract carries the current-period stock columns twice under
# near-identical labels; this reads the first occurrence of each, matching
# raw_mard's column order. See MARD_FIELDS in field_map for the full mapping
# and why only these six columns are staged.
_STOCK_SQL = """
    SELECT material, plant, storage_location,
           unrestricted, stock_in_transfer, in_quality_insp,
           restricted_use_stock, blocked, returns
      FROM raw_mard
"""


def _stage_stock(
    session: Session, run_id: int, policy: ExtractIngestionPolicy, rejections: _Rejections
) -> int:
    def rows() -> Iterator[dict[str, Any]]:
        for row in _stream(session, _STOCK_SQL):
            material = clean(row.material)
            plant = clean(row.plant)
            storage_location = clean(row.storage_location)

            if material is None:
                rejections.add("raw_mard", None, RejectionReason.MISSING_MATERIAL)
                continue
            if plant is None:
                rejections.add("raw_mard", material, RejectionReason.MISSING_PLANT)
                continue
            if storage_location is None:
                # Not a documented rejection reason of its own: MARD's key
                # requires a storage location, so a missing one is the same
                # kind of gap as a missing plant.
                rejections.add("raw_mard", material, RejectionReason.MISSING_PLANT)
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
                "source_table": "raw_mard",
                "staging_run_id": run_id,
            }

    staged = 0
    for batch in _batched(rows(), _safe_batch_size(StagedStock, policy.batch_size)):
        _upsert(
            session,
            StagedStock,
            batch,
            ["sap_material_number", "sap_plant_code", "storage_location"],
        )
        staged += len(batch)
    return staged


# --- Consumption -------------------------------------------------------

# Aggregated in SQL. Issues count positive and reversals negative via SHKZG, so
# a cancelled issue nets out instead of inflating demand.
#
# date_trunc yields a timestamp; the ::date cast makes it the first of the month.
_CONSUMPTION_SQL = """
    SELECT material AS sap_material_number,
           plant AS sap_plant_code,
           date_trunc('month', posting_date::date)::date AS period,
           SUM(CASE WHEN debit_credit_ind = :credit THEN quantity::numeric
                    ELSE -quantity::numeric END) AS quantity,
           MAX(base_unit_of_measure) AS unit_of_measure,
           COUNT(*) AS movement_count,
           COUNT(*) FILTER (WHERE movement_type = ANY(:issue_types)) AS issue_count,
           COUNT(*) FILTER (WHERE movement_type = ANY(:reversal_types)) AS reversal_count
      FROM raw_mseg
     WHERE movement_type = ANY(:movement_types)
       AND NULLIF(material, '') IS NOT NULL
       AND NULLIF(plant, '') IS NOT NULL
       AND posting_date ~ '^\\d{4}-\\d{2}-\\d{2}$'
       AND quantity ~ '^-?[0-9]+(\\.[0-9]+)?$'
     GROUP BY 1, 2, 3
"""
# issue_count/reversal_count split by movement TYPE (201/261 vs 202/262), not
# by debit_credit_ind -- the SOP 3.1.1 trigger needs a transaction-level event
# count, and movement type is what the ingestion policy's issue/reversal sets
# are defined over.

# Rows the aggregate above excludes, counted so nothing vanishes unexplained.
_CONSUMPTION_REJECT_SQL = """
    SELECT CASE
             WHEN NULLIF(material, '') IS NULL THEN :missing_material
             WHEN NULLIF(plant, '') IS NULL THEN :missing_plant
             WHEN posting_date !~ '^\\d{4}-\\d{2}-\\d{2}$' THEN :invalid_date
             ELSE :invalid_quantity
           END AS reason,
           COUNT(*) AS n
      FROM raw_mseg
     WHERE movement_type = ANY(:movement_types)
       AND (NULLIF(material, '') IS NULL
            OR NULLIF(plant, '') IS NULL
            OR posting_date !~ '^\\d{4}-\\d{2}-\\d{2}$'
            OR quantity !~ '^-?[0-9]+(\\.[0-9]+)?$')
     GROUP BY 1
"""


def _stage_consumption(
    session: Session, run_id: int, policy: ExtractIngestionPolicy, rejections: _Rejections
) -> int:
    movement_types = list(policy.consumption.all_movement_types)

    for row in session.execute(
        text(_CONSUMPTION_REJECT_SQL),
        {
            "movement_types": movement_types,
            "missing_material": RejectionReason.MISSING_MATERIAL,
            "missing_plant": RejectionReason.MISSING_PLANT,
            "invalid_date": RejectionReason.INVALID_DATE,
            "invalid_quantity": RejectionReason.INVALID_QUANTITY,
        },
    ):
        rejections.counts[row.reason] = rejections.counts.get(row.reason, 0) + row.n
        rejections.add(
            "raw_mseg", None, row.reason, f"{row.n} movement rows excluded by this rule"
        )

    def rows() -> Iterator[dict[str, Any]]:
        for row in _stream(
            session,
            _CONSUMPTION_SQL,
            {
                "movement_types": movement_types,
                "credit": CREDIT_INDICATOR,
                "issue_types": list(policy.consumption.issue_movement_types),
                "reversal_types": list(policy.consumption.reversal_movement_types),
            },
        ):
            yield {
                "sap_material_number": row.sap_material_number,
                "sap_plant_code": row.sap_plant_code,
                "period": row.period,
                "quantity": row.quantity,
                "unit_of_measure": row.unit_of_measure,
                "movement_count": row.movement_count,
                "issue_count": row.issue_count,
                "reversal_count": row.reversal_count,
                "source_table": "raw_mseg",
                "staging_run_id": run_id,
            }

    staged = 0
    for batch in _batched(rows(), _safe_batch_size(StagedConsumption, policy.batch_size)):
        _upsert(
            session,
            StagedConsumption,
            batch,
            ["sap_material_number", "sap_plant_code", "period"],
        )
        staged += len(batch)
    return staged


# --- Purchase orders ---------------------------------------------------

# One row per PO line, with the earliest goods receipt and earliest schedule
# date folded in. Both are aggregated in subqueries so the join stays 1:1 --
# a PO line can have several receipts and several schedule lines.
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
           k.deletion_indicator AS header_deletion,
           g.goods_receipt_date,
           g.quantity_received,
           s.planned_delivery_date
      FROM raw_ekpo p
      LEFT JOIN raw_ekko k ON k.purchasing_document = p.purchasing_document
      LEFT JOIN (
            SELECT purchasing_document, item,
                   MIN(posting_date) AS goods_receipt_date,
                   SUM(quantity::numeric) AS quantity_received
              FROM raw_ekbe
             WHERE po_history_category = :gr_category
               AND movement_type = :gr_movement
               AND posting_date ~ '^\\d{4}-\\d{2}-\\d{2}$'
               AND quantity ~ '^-?[0-9]+(\\.[0-9]+)?$'
             GROUP BY purchasing_document, item
      ) g ON g.purchasing_document = p.purchasing_document AND g.item = p.item
      LEFT JOIN (
            SELECT purchasing_document, item, MIN(delivery_date) AS planned_delivery_date
              FROM raw_eket
             WHERE delivery_date ~ '^\\d{4}-\\d{2}-\\d{2}$'
             GROUP BY purchasing_document, item
      ) s ON s.purchasing_document = p.purchasing_document AND s.item = p.item
     WHERE NULLIF(p.material, '') IS NOT NULL
"""


def _stage_purchase_orders(
    session: Session, run_id: int, policy: ExtractIngestionPolicy, rejections: _Rejections
) -> int:
    def rows() -> Iterator[dict[str, Any]]:
        for row in _stream(
            session,
            _PURCHASE_ORDER_SQL,
            {
                "gr_category": GOODS_RECEIPT_HISTORY_CATEGORY,
                "gr_movement": GOODS_RECEIPT_MOVEMENT_TYPE,
            },
        ):
            document = clean(row.purchasing_document)
            item = clean(row.item)
            material = clean(row.material)
            plant = clean(row.plant)

            if document is None or item is None:
                rejections.add("raw_ekpo", material, RejectionReason.MISSING_PURCHASING_DOCUMENT)
                continue
            key = f"{document}/{item}"
            if material is None:
                rejections.add("raw_ekpo", key, RejectionReason.MISSING_MATERIAL)
                continue
            if plant is None:
                rejections.add("raw_ekpo", key, RejectionReason.MISSING_PLANT)
                continue

            created_on = parse_date(row.created_on)
            goods_receipt = parse_date(row.goods_receipt_date)

            lead_time = None
            if created_on is not None and goods_receipt is not None:
                delta = (goods_receipt - created_on).days
                if delta < 0:
                    # Impossible, so the pair is untrustworthy. The line still
                    # stages -- it is a real PO -- but with no lead time.
                    rejections.add(
                        "raw_ekpo",
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
                "planned_delivery_date": parse_date(row.planned_delivery_date),
                "planned_delivery_time_days": parse_int(row.planned_deliv_time),
                "quantity_ordered": parse_decimal(row.order_quantity),
                "quantity_received": parse_decimal(row.quantity_received),
                "supplier": clean(row.supplier),
                "is_cancelled": bool(cancelled),
                "lead_time_days": lead_time,
                "source_table": "raw_ekpo",
                "staging_run_id": run_id,
            }

    staged = 0
    for batch in _batched(rows(), _safe_batch_size(StagedPurchaseOrder, policy.batch_size)):
        _upsert(session, StagedPurchaseOrder, batch, ["purchasing_document", "item"])
        staged += len(batch)
    return staged


# --- Entry point -------------------------------------------------------


def stage_extract(policy: ExtractIngestionPolicy | None = None) -> StagingResult:
    """Normalise the seeded July/August extract into canonical staging.

    Idempotent: running twice converges on the same state. Raw tables are only
    ever read.
    """
    policy = policy or ExtractIngestionPolicy()
    session_factory = get_sessionmaker()

    with session_factory() as session:
        run = StagingRun(
            source=SOURCE_JULY_EXTRACT,
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

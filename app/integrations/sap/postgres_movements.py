"""Goods-movement history from Postgres, for W3.5 (statistics-independent aging).

Reads ``raw_mseg`` (MSEG-equivalent, one row per movement line) joined to
``raw_mkpf`` (MKPF-equivalent, the document header carrying the posting date)
on the document key SAP itself uses: material document number + material
document year (``material_document`` + ``material_doc_year``). Both tables
were loaded from the real SAP extract by ``app.seed.loader`` (see
``app/seed/manifest.py``) -- this module is a read-only consumer, never a
second data source: no CSV, no generated fixtures, no in-memory fallback.

Rows are normalized into the exact same shape
(``Matnr``/``Werks``/``Bwart``/``Menge``/``BudatMkpf``) that
``app.initiatives.i13.movements`` already consumes for the CSV-backed path,
so the reversal-netting, windowing and aggregation logic there is reused
unchanged rather than re-implemented against Postgres.

Known data-quality gap in this extract, filtered out rather than guessed at
(see the W3.5 implementation report for the measured scale): a majority of
receipt (101) rows carry a blank ``material`` -- these cannot be attributed
to any (material, plant) grain and are excluded, same as a row with no
posting date or no plant. Goods-issue rows (201/261), which is what W3.5's
aging classification actually keys off (see ``movement_metrics.py``), are
unaffected -- ``material`` is populated on all of them in this dataset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

Row = dict[str, Any]

_MOVEMENT_HISTORY_QUERY = """
    SELECT m.material, m.plant, m.movement_type, m.quantity, h.posting_date
    FROM raw_mseg m
    JOIN raw_mkpf h
      ON m.material_document = h.material_document
     AND m.material_doc_year = h.material_doc_year
    WHERE m.material <> '' AND m.plant <> '' AND h.posting_date <> ''
      {material_filter}
      {plant_filter}
"""

# Only ``unrestricted`` (raw_mard's LABST-equivalent) -- the same single
# stock component ``app.initiatives.i13.watch`` already treats as
# "current_stock" for the CSV-backed path (see ``_sum_stock_by_material_plant``,
# which reads only ``Labst``, never quality-inspection or blocked stock).
# Reused here rather than redefined, so a material's inventory-turns
# denominator means the same thing regardless of which gateway computed it.
_CURRENT_STOCK_QUERY = """
    SELECT material, plant, unrestricted
    FROM raw_mard
    WHERE material <> '' AND plant <> '' AND unrestricted <> ''
      {material_filter}
      {plant_filter}
"""


def _decimal(raw: str | None) -> Decimal:
    if raw in (None, ""):
        return Decimal("0")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return Decimal("0")


def _to_movement_row(record: Any) -> Row:
    """One joined ``raw_mseg``/``raw_mkpf`` record -> the normalized Row
    shape ``app.initiatives.i13.movements`` already knows how to net/window."""
    return {
        "Matnr": record.material.strip(),
        "Werks": record.plant.strip(),
        "Bwart": (record.movement_type or "").strip(),
        "Menge": _decimal(record.quantity),
        "BudatMkpf": date.fromisoformat(record.posting_date),
    }


def fetch_movement_history(
    db: Session, *, material: str | None = None, plant: str | None = None
) -> list[Row]:
    """Real goods-movement rows from Postgres, normalized for W3.5.

    Filters push down to SQL when a single material/plant is requested (the
    per-item API) rather than pulling the full ~230k-row history into Python
    just to throw most of it away.
    """
    params: dict[str, str] = {}
    material_filter = ""
    if material:
        material_filter = "AND m.material = :material"
        params["material"] = material
    plant_filter = ""
    if plant:
        plant_filter = "AND m.plant = :plant"
        params["plant"] = plant

    query = text(_MOVEMENT_HISTORY_QUERY.format(material_filter=material_filter, plant_filter=plant_filter))
    records = db.execute(query, params).fetchall()
    return [_to_movement_row(record) for record in records]


def fetch_current_stock(
    db: Session, *, material: str | None = None, plant: str | None = None
) -> dict[tuple[str, str], Decimal]:
    """Current unrestricted-use stock per (material, plant), summed across
    storage locations. A point-in-time snapshot (``raw_mard`` carries no
    history) -- see ``movement_metrics.py`` for how that limits inventory
    turns to a current-stock proxy rather than a true trailing-average one.
    """
    params: dict[str, str] = {}
    material_filter = ""
    if material:
        material_filter = "AND material = :material"
        params["material"] = material
    plant_filter = ""
    if plant:
        plant_filter = "AND plant = :plant"
        params["plant"] = plant

    query = text(_CURRENT_STOCK_QUERY.format(material_filter=material_filter, plant_filter=plant_filter))
    records = db.execute(query, params).fetchall()

    totals: dict[tuple[str, str], Decimal] = {}
    for record in records:
        key = (record.material.strip(), record.plant.strip())
        totals[key] = totals.get(key, Decimal("0")) + _decimal(record.unrestricted)
    return totals


@dataclass
class PostgresMovementRepository:
    """Thin object wrapper so callers (the domain service, the API layer)
    depend on a repository, not on bare functions plus a session they have
    to remember to pass consistently. Holds a session, nothing else."""

    db: Session

    def get_movement_history(self, *, material: str | None = None, plant: str | None = None) -> list[Row]:
        return fetch_movement_history(self.db, material=material, plant=plant)

    def get_current_stock(
        self, *, material: str | None = None, plant: str | None = None
    ) -> dict[tuple[str, str], Decimal]:
        return fetch_current_stock(self.db, material=material, plant=plant)

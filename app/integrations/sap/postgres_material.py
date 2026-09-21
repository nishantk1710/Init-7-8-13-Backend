"""MRP type (DISMM) from Postgres, for W6.2's OAR material-scope filter.

Reads ``raw_marc`` (MARC-equivalent) -- key (material, plant) is 100% unique
in this dataset (45,409 rows, measured). This module supplies only the raw
DISMM string per (material, plant); classifying it OAR/Min-Max/Excluded is
``app.shared.material_scope.policy.classify_material_scope`` -- the existing
W2.4 rule, reused unchanged, never reimplemented here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

_MRP_TYPE_QUERY = """
    SELECT material, plant, mrp_type
    FROM raw_marc
    WHERE material <> '' AND plant <> ''
      {material_filter}
      {plant_filter}
"""


def fetch_material_scope_index(
    db: Session, *, material: str | None = None, plant: str | None = None
) -> dict[tuple[str, str], str | None]:
    """(material, plant) -> raw DISMM string (possibly blank/None), for
    ``classify_material_scope`` to classify. Not itself a scope decision."""
    params: dict[str, Any] = {}
    material_filter = ""
    if material:
        material_filter = "AND material = :material"
        params["material"] = material
    plant_filter = ""
    if plant:
        plant_filter = "AND plant = :plant"
        params["plant"] = plant

    query = text(_MRP_TYPE_QUERY.format(material_filter=material_filter, plant_filter=plant_filter))
    records = db.execute(query, params).fetchall()
    return {(r.material.strip(), r.plant.strip()): (r.mrp_type or "").strip() or None for r in records}

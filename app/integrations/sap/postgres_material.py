"""MRP controller (DISPO) from the database, for the OAR material-scope filter.

Reads ``n_marc`` (MARC-equivalent) -- key (material, plant) is 100% unique
in this dataset (45,409 rows, measured). This module supplies only the raw
DISPO string per (material, plant); classifying it OAR/Excluded is
``app.shared.material_scope.policy.classify_material_scope`` -- reused
unchanged, never reimplemented here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.shared.material_scope.deletion import deleted_column
from app.shared.plant_scope import sql_predicate

# Plants 1300 and 1500 only -- the team lead's ruling of 2026-09-21. Applied in
# the WHERE clause rather than over the returned rows so counts, sums and aging
# bands are computed on the scoped population to begin with; filtering after
# aggregation is where an out-of-scope quantity leaks into a total. Built from
# app/shared/plant_scope.py -- never write the codes out here.
_PLANT_SCOPE = sql_predicate("plant")



_SCOPE_QUERY = """
    SELECT material, plant, mrp_type, mrp_controller, {deleted_column}
    FROM n_marc mc
    WHERE material <> '' AND plant <> '' AND {plant_scope}
      {material_filter}
      {plant_filter}
"""


def fetch_material_scope_index(
    db: Session,
    *,
    material: str | None = None,
    plant: str | None = None,
    deleted: set[tuple[str, str]] | None = None,
    mrp_types: dict[tuple[str, str], str] | None = None,
) -> dict[tuple[str, str], str | None]:
    """(material, plant) -> raw DISPO (MRP controller) string, for
    ``classify_material_scope`` to classify. Not itself a scope decision.

    A MARC row with no MRP controller maps to ``""``, never ``None``: both
    classify as not OAR, but ``""`` says MARC has the row, which the assistant's
    reason needs to tell apart from a data gap.

    ``mrp_types``, when passed, is filled with each returned pair's MRP type
    (DISMM) -- no longer the rule, but still what the assistant records and
    shows.

    A pair flagged for deletion in SAP (MARC/MARA-LVORM, see
    ``app.shared.material_scope.deletion``) is left out -- so it classifies
    EXCLUDED like a missing row -- and added to ``deleted`` when the caller
    passes a set, so it can say why. One query either way."""
    params: dict[str, Any] = {}
    material_filter = ""
    if material:
        material_filter = "AND material = :material"
        params["material"] = material
    plant_filter = ""
    if plant:
        plant_filter = "AND plant = :plant"
        params["plant"] = plant

    query = text(
        _SCOPE_QUERY.format(
            material_filter=material_filter,
            plant_filter=plant_filter,
            plant_scope=_PLANT_SCOPE,
            deleted_column=deleted_column("mc"),
        )
    )
    records = db.execute(query, params).fetchall()
    index: dict[tuple[str, str], str | None] = {}
    for r in records:
        key = (r.material.strip(), r.plant.strip())
        if getattr(r, "deleted", 0):
            if deleted is not None:
                deleted.add(key)
            continue
        index[key] = (r.mrp_controller or "").strip()
        if mrp_types is not None:
            mrp_types[key] = (r.mrp_type or "").strip()
    return index

"""OAR / Min-Max / Excluded material-scope classification.

The team lead's ruling of 2026-09-08 identifies OAR ("Planned on Demand")
materials by MRP type -- ``MaterialPlantSet.Dismm`` -- rather than the older
``MaterialSet.Extwg`` field, which is no longer read anywhere. ``Dismm`` is
plant-level (MARC), so scope is decided per material *and* plant: the same
material can be OAR in one plant and Min-Max in another.

The MRP type codes that map to each scope are configuration
(``Settings.i13_oar_mrp_types`` / ``i13_min_max_mrp_types``), not a hard-coded
rule, so the business can retune the ruling without a code change. This
module is shared platform infrastructure -- consumers must call
``classify_material_scope`` rather than re-implementing the predicate.
"""

from enum import Enum
from typing import Any

from app.core.config import get_settings

Row = dict[str, Any]


class MaterialScope(str, Enum):
    """Result of classifying a material+plant by MRP type (DISMM)."""

    OAR = "OAR"
    MIN_MAX = "MIN_MAX"
    EXCLUDED = "EXCLUDED"


def classify_material_scope(dismm: str | None) -> MaterialScope:
    """Classify a material+plant's OAR/Min-Max/Excluded scope from its DISMM.

    Normalises whitespace and case before matching. Anything not explicitly
    configured as OAR or Min-Max -- including blank and ``None`` -- is
    ``EXCLUDED``.
    """
    settings = get_settings()
    normalized = (dismm or "").strip().upper()

    if not normalized:
        return MaterialScope.EXCLUDED
    if normalized in settings.i13_oar_mrp_type_set:
        return MaterialScope.OAR
    if normalized in settings.i13_min_max_mrp_type_set:
        return MaterialScope.MIN_MAX
    return MaterialScope.EXCLUDED


def build_scope_index(material_plants: list[Row]) -> dict[tuple[str, str], MaterialScope]:
    """(material, plant) -> ``MaterialScope`` for every ``MaterialPlantSet`` row.

    The one place this index is built -- every I13 consumer that needs to
    filter or annotate by OAR/Min-Max/Excluded scope (exceptions, the ledger
    API boundary, reclassification) looks up into this rather than
    re-deriving it from ``Dismm`` at each call site.
    """
    return {
        (row.get("Matnr"), row.get("Werks")): classify_material_scope(row.get("Dismm"))
        for row in material_plants
        if row.get("Matnr") is not None and row.get("Werks") is not None
    }

"""OAR / Excluded material-scope classification.

The I13 ruling of 2026-11-21 identifies OAR materials by MRP type --
``MARC.DISMM`` -- in ``ND`` or ``PD``, with the material not flagged for
deletion (MARC-LVORM and MARA-LVORM blank). It replaces the ruling of
2026-10-20, which read the MRP controller (``MARC.DISPO = OAR``); DISPO plays
no part in the I13 rule any more. Unlike the September DISMM rule, a blank
DISMM is **not** OAR. ``DISMM`` is plant-level (MARC), so scope is decided per
material *and* plant: the same material can be OAR in one plant and not in
another.

The MRP types that count as OAR are configuration
(``Settings.i13_oar_mrp_types``), not a hard-coded rule, so the business can
retune the ruling without a code change. Materials flagged for deletion never
reach this function: the index readers leave them out
(``app.shared.material_scope.deletion``), so they classify like a missing row.
This module is shared platform infrastructure for I13 and the assistant's
routing -- consumers must call ``classify_material_scope`` rather than
re-implementing the predicate. I07 keeps its own rule
(``app.initiatives.i7.policy.oar``).
"""

from enum import Enum
from typing import Any

from app.core.config import get_settings

Row = dict[str, Any]


class MaterialScope(str, Enum):
    """Result of classifying a material+plant by MRP type (DISMM)."""

    OAR = "OAR"
    MIN_MAX = "MIN_MAX"
    """Not produced by the current rule. Kept so stored and served values that
    name it still parse."""
    EXCLUDED = "EXCLUDED"


def classify_material_scope(mrp_type: str | None) -> MaterialScope:
    """Classify a material+plant's OAR/Excluded scope from its DISMM.

    Normalises whitespace and case before matching. ``None`` means MARC has no
    row for the material+plant (or it is flagged for deletion) and is
    ``EXCLUDED``; so is a blank DISMM and any MRP type not configured as OAR.
    """
    if mrp_type is None:
        return MaterialScope.EXCLUDED
    if mrp_type.strip().upper() in get_settings().i13_oar_mrp_type_set:
        return MaterialScope.OAR
    return MaterialScope.EXCLUDED


def build_scope_index(material_plants: list[Row]) -> dict[tuple[str, str], MaterialScope]:
    """(material, plant) -> ``MaterialScope`` for every ``MaterialPlantSet`` row.

    The one place this index is built from OData-shaped rows -- every I13
    consumer that needs to filter or annotate by OAR scope looks up into this
    rather than re-deriving it from ``Dismm`` at each call site.
    """
    return {
        (row.get("Matnr"), row.get("Werks")): classify_material_scope(row.get("Dismm"))
        for row in material_plants
        if row.get("Matnr") is not None and row.get("Werks") is not None
    }

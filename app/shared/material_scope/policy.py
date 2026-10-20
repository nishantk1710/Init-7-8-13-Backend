"""OAR / Excluded material-scope classification.

The VZI ruling of 2026-10-20 identifies OAR materials by MRP controller --
``MARC.DISPO`` -- equal to ``OAR``. It replaces the team lead's ruling of
2026-09-08, which read the MRP type (``MARC.DISMM`` in ND/PD, blank included);
that rule counted ~96% of the catalogue as OAR, where ``DISPO = OAR`` is the
population the business actually plans on demand. (The older
``MaterialSet.Extwg`` field is read nowhere.) ``DISPO`` is plant-level (MARC),
so scope is decided per material *and* plant: the same material can be OAR in
one plant and not in another.

The controller codes that count as OAR are configuration
(``Settings.i13_oar_mrp_controllers``), not a hard-coded rule, so the business
can retune the ruling without a code change. A blank DISPO is not OAR. Materials
flagged for deletion never reach this function: the index readers leave them
out (``app.shared.material_scope.deletion``), so they classify like a missing
row. This module is shared platform infrastructure -- consumers must call
``classify_material_scope`` rather than re-implementing the predicate.
"""

from enum import Enum
from typing import Any

from app.core.config import get_settings

Row = dict[str, Any]


class MaterialScope(str, Enum):
    """Result of classifying a material+plant by MRP controller (DISPO)."""

    OAR = "OAR"
    MIN_MAX = "MIN_MAX"
    """No longer produced: the DISPO rule has no Min-Max code. Kept so stored
    and served values that name it still parse."""
    EXCLUDED = "EXCLUDED"


def classify_material_scope(mrp_controller: str | None) -> MaterialScope:
    """Classify a material+plant's OAR/Excluded scope from its DISPO.

    Normalises whitespace and case before matching. ``None`` means MARC has no
    row for the material+plant (or it is flagged for deletion) and is
    ``EXCLUDED``; so is a blank DISPO and any code not configured as OAR.
    """
    if mrp_controller is None:
        return MaterialScope.EXCLUDED
    if mrp_controller.strip().upper() in get_settings().i13_oar_mrp_controller_set:
        return MaterialScope.OAR
    return MaterialScope.EXCLUDED


def build_scope_index(material_plants: list[Row]) -> dict[tuple[str, str], MaterialScope]:
    """(material, plant) -> ``MaterialScope`` for every ``MaterialPlantSet`` row.

    The one place this index is built from OData-shaped rows -- every I13
    consumer that needs to filter or annotate by OAR scope looks up into this
    rather than re-deriving it from ``Dispo`` at each call site.
    """
    return {
        (row.get("Matnr"), row.get("Werks")): classify_material_scope(row.get("Dispo"))
        for row in material_plants
        if row.get("Matnr") is not None and row.get("Werks") is not None
    }

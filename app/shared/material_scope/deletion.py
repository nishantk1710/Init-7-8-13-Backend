"""Materials flagged for deletion in SAP are not OAR.

UAT ruling, 19 Oct 2026: a material whose deletion flag is set -- MARC-LVORM
(deleted at that plant) or MARA-LVORM (deleted at client level, so at every
plant) equal to ``X`` -- is left out of the OAR population, whatever its MRP
type says.

It is applied where the (material, plant) -> MRP type lookup is read, never
afterwards: a pair missing from that lookup already classifies as
``EXCLUDED`` (``classify_material_scope(None)``), so every OAR screen, KPI and
the assistant's routing drop the material through the one path they already
share.

``X`` is the CSV extract's encoding, which is what ``n_marc`` / ``n_mara`` are
built from. (OData's ``MaterialPlantSet.Lvorm`` spells it ``1`` -- see
``app.initiatives.i7.adapters.field_map`` -- but I13 does not read OData.)

Switch: ``Settings.i13_exclude_deleted_materials`` (on by default).
"""

from __future__ import annotations

from app.core.config import get_settings

DELETION_FLAG = "X"


def _flag(column: str) -> str:
    return f"UPPER(LTRIM(RTRIM(COALESCE({column}, '')))) = '{DELETION_FLAG}'"


def deleted_condition(marc: str) -> str:
    """SQL, true when the ``n_marc`` row aliased ``marc`` is flagged for
    deletion at its plant or its material is flagged at client level.

    EXISTS rather than a join: a duplicated MARA row must never multiply the
    MARC rows it is checked against.
    """
    return (
        f"({_flag(f'{marc}.df_at_plant_level')} OR EXISTS (SELECT 1 FROM n_mara deleted_mara "
        f"WHERE deleted_mara.material = {marc}.material AND {_flag('deleted_mara.df_at_client_level')}))"
    )


def exclude_deleted() -> bool:
    return get_settings().i13_exclude_deleted_materials


def not_deleted_predicate(marc: str) -> str:
    """``AND NOT (...)`` for a WHERE clause over ``n_marc`` aliased ``marc``;
    empty when the exclusion is switched off."""
    return f"AND NOT {deleted_condition(marc)}" if exclude_deleted() else ""


def deleted_column(marc: str) -> str:
    """A ``deleted`` select column (1/0) for ``n_marc`` aliased ``marc``, so a
    caller can say *why* a pair is out rather than just dropping it. Always 0
    when the exclusion is switched off."""
    if not exclude_deleted():
        return "0 AS deleted"
    return f"CASE WHEN {deleted_condition(marc)} THEN 1 ELSE 0 END AS deleted"

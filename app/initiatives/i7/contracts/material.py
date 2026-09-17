"""Canonical material-plant attributes.

Source-independent by construction: the July extract, live OData and a future
RFC feed all produce this same shape, so nothing downstream knows which one it
came from.

Every business field is optional except identity. That is not laxity -- it is
what the data looks like. Criticality reaches only 24% of materials, MSTAE and
EXTWG come from MARA which covers 8% of the MARC population, and circuit has no
source at all. A contract that required them could not represent the majority of
the real catalogue, so absence is modelled as ``None`` and the policy layer
decides what an absent value means.
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.initiatives.i7.contracts.enums import Criticality
from app.initiatives.i7.contracts.identity import MaterialPlantKey


class MaterialAttributes(BaseModel):
    """What I07 needs to know about one material at one plant."""

    model_config = ConfigDict(frozen=True)

    key: MaterialPlantKey

    # --- Fields the OAR rule reads -------------------------------------
    # Both are inputs to scope evaluation. Neither is interpreted here: the
    # meaning of a value, and of its absence, belongs to the OAR policy.

    mrp_type: str | None = None
    """MARC.DISMM. ``None`` means not maintained -- which is *unknown*, not
    "not OAR". 47% of rows in the live scan had no value."""

    material_status: str | None = None
    """MARA.MSTAE. ``'01'`` marks an obsolete material and excludes it from OAR
    under the current rule; that comparison lives in the policy, not here."""

    external_material_group: str | None = None
    """MARA.EXTWG.

    RETIRED as an OAR identifier. Carried for source fidelity and audit only --
    it is in the extract, so dropping it would lose data the contract is
    supposed to mirror faithfully. The active OAR rule must not reference it,
    and :class:`~app.initiatives.i7.policy.oar.OarPolicy` refuses to build a
    predicate on it."""

    # --- Business attributes -------------------------------------------

    criticality: Criticality | None = None
    """ZMM065 tier. Absent for ~76% of MARC materials."""

    circuit: str | None = None
    """Processing circuit (Milling, Crushing...).

    No SAP field and no platform file supplies this today, yet the service-level
    matrix is keyed on Criticality x Circuit and OAR similarity scores it. Left
    as a free string rather than an enum: the valid set is a VZI reference-data
    question, and inventing one here would be inventing business data."""

    material_group: str | None = None
    """MARA.MATKL. A Gower feature for OAR similarity."""

    base_unit_of_measure: str | None = None
    manufacturer: str | None = None
    """MARA manufacturer, for the OEM-match similarity feature."""

    unit_price: Decimal | None = None
    """MBEW moving average price. ``Decimal``, not ``float``: money.

    ``None`` means unknown. It must never be coerced to zero -- a zero price
    silently zeroes EOQ and working-capital impact, which is how a missing value
    turns into a confident wrong number."""

    currency: str | None = None

    # --- Current SAP planning parameters --------------------------------
    # What SAP holds today, so a recommendation can show current vs recommended.
    # Absent means not maintained, which is itself meaningful: only ~2% of MARC
    # rows carry a reorder point at all.

    current_safety_stock: Decimal | None = None
    """MARC.EISBE."""

    current_reorder_point: Decimal | None = None
    """MARC.MINBE."""

    current_maximum_stock: Decimal | None = None
    """MARC.MABST."""

    planned_delivery_time_days: int | None = Field(default=None, ge=0)
    """MARC.PLIFZ. The documented fallback when PO history is too thin."""

    deletion_flag: bool | None = None
    """MARA.LVORM / MARC.LVORM -- flagged for deletion."""

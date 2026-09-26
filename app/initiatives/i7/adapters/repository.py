"""Staging rows -> canonical contracts.

Phase 3 reads through here rather than querying tables itself, so the domain
never sees a SQLAlchemy row and never learns a column name. The staging schema
can then change without touching classification or forecasting.

Consumption is where this earns its place. Staging stores only months that had
movements -- a zero row is the *absence* of a movement, and materialising every
month for every material-plant would be a large, mostly-empty table. But
``ADI = n / n_nz`` counts total periods, so the series handed to a caller must
be dense.

**The window is the extract's, not the material's own first-to-last movement.**
:func:`consumption_series_for` used to densify over each material's own
observed range -- which sounds like the more conservative choice, but is
wrong: a material whose first movement is late loses its leading zero-demand
months from ``n`` while keeping every one of its ``n_nz``, silently
understating ADI. Measured on this extract: 3,495 of 4,269 material-plants
started late, one example moving ADI from 1.500 to 1.625 once the leading
zeros are counted. Those leading months are real evidence of *no demand*,
which is exactly what an intermittency measure needs to see -- see
:func:`app.initiatives.i7.features.builder._densify` for the full rationale.

So this module now shares Phase 3's own densification (:func:`_densify`) and
its extract-wide window (:func:`observation_window`) rather than reimplementing
either. Forecasting (the only caller of :func:`consumption_series_for`) and
the feature store must agree on ``total_periods``/``non_zero_periods`` for the
same material-plant -- a second, independently-windowed implementation is
exactly how they drift apart.
"""

from typing import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.initiatives.i7.contracts import (
    ConsumptionSeries,
    MaterialAttributes,
    MaterialIdentity,
    MaterialPlantKey,
    PlantIdentity,
    PurchaseOrderObservation,
)
from app.initiatives.i7.contracts.enums import Criticality
from app.initiatives.i7.features.builder import _densify, observation_window
from app.models.i7_staging import (
    StagedConsumption,
    StagedMaterial,
    StagedMaterialPlant,
    StagedPurchaseOrder,
)


def _criticality(raw: str | None) -> Criticality | None:
    """ZMM065 text to the enum. Unrecognised stays ``None``.

    An unexpected tier is not silently coerced to NORMAL: that would invent a
    criticality, and criticality drives the service level.
    """
    if raw is None:
        return None
    try:
        return Criticality(raw.strip().upper())
    except ValueError:
        return None


def _key(material: str, plant: str, description: str | None = None) -> MaterialPlantKey:
    return MaterialPlantKey(
        material=MaterialIdentity(
            sap_material_number=material,
            # Unresolved -- no SAP field supplies the app-side identity.
            app_material_id=None,
            description=description,
        ),
        plant=PlantIdentity(sap_plant_code=plant, app_plant_id=None),
    )


def material_attributes_for(
    session: Session, material: str, plant: str
) -> MaterialAttributes | None:
    """Canonical attributes for one material-plant, or ``None`` if not staged."""
    plant_row = session.execute(
        select(StagedMaterialPlant).where(
            StagedMaterialPlant.sap_material_number == material,
            StagedMaterialPlant.sap_plant_code == plant,
        )
    ).scalar_one_or_none()
    if plant_row is None:
        return None

    material_row = session.execute(
        select(StagedMaterial).where(StagedMaterial.sap_material_number == material)
    ).scalar_one_or_none()

    return _build_attributes(plant_row, material_row)


def _build_attributes(
    plant_row: StagedMaterialPlant, material_row: StagedMaterial | None
) -> MaterialAttributes:
    return MaterialAttributes(
        key=_key(
            plant_row.sap_material_number,
            plant_row.sap_plant_code,
            material_row.description if material_row else None,
        ),
        mrp_type=plant_row.mrp_type,
        material_status=material_row.material_status if material_row else None,
        external_material_group=(
            material_row.external_material_group if material_row else None
        ),
        criticality=_criticality(material_row.criticality) if material_row else None,
        # No SAP source. Stays None until VZI supplies plant reference data.
        circuit=None,
        material_group=material_row.material_group if material_row else None,
        base_unit_of_measure=material_row.base_unit_of_measure if material_row else None,
        manufacturer=material_row.manufacturer if material_row else None,
        unit_price=material_row.unit_price if material_row else None,
        currency=material_row.currency if material_row else None,
        current_safety_stock=plant_row.current_safety_stock,
        current_reorder_point=plant_row.current_reorder_point,
        current_maximum_stock=plant_row.current_maximum_stock,
        planned_delivery_time_days=plant_row.planned_delivery_time_days,
        deletion_flag=plant_row.deletion_flag,
    )


def iter_material_attributes(
    session: Session, *, plant: str | None = None, batch_size: int = 1000
) -> Iterator[MaterialAttributes]:
    """Stream every staged material-plant as a canonical contract.

    Streams rather than returning a list: 45,409 material-plants is small today
    but the pattern must not assume that.
    """
    statement = (
        select(StagedMaterialPlant, StagedMaterial)
        .join(
            StagedMaterial,
            StagedMaterial.sap_material_number == StagedMaterialPlant.sap_material_number,
            isouter=True,
        )
        .order_by(
            StagedMaterialPlant.sap_material_number, StagedMaterialPlant.sap_plant_code
        )
    )
    if plant is not None:
        statement = statement.where(StagedMaterialPlant.sap_plant_code == plant)

    for plant_row, material_row in session.execute(statement).yield_per(batch_size):
        yield _build_attributes(plant_row, material_row)


def consumption_series_for(
    session: Session, material: str, plant: str
) -> ConsumptionSeries | None:
    """The dense monthly series for one material-plant.

    Densified over the **extract-wide** observation window -- the same window
    and the same densification (:func:`app.initiatives.i7.features.builder._densify`)
    the feature store uses, so ``total_periods``/``non_zero_periods`` for a
    material-plant agree between the two regardless of which one a caller
    reads. See this module's docstring for why a per-material window would be
    wrong, not just different.

    Returns ``None`` when either nothing is staged for this material-plant, or
    the extract itself has no observation window at all (no consumption
    staged anywhere) -- there is no window to densify over in either case.
    """
    window = observation_window(session)
    if window is None:
        return None

    rows = session.execute(
        select(
            StagedConsumption.period,
            StagedConsumption.quantity,
            StagedConsumption.unit_of_measure,
        )
        .where(
            StagedConsumption.sap_material_number == material,
            StagedConsumption.sap_plant_code == plant,
        )
        .order_by(StagedConsumption.period)
    ).all()

    if not rows:
        return None

    return _densify(rows, _key(material, plant), window)


def purchase_orders_for(
    session: Session, material: str, plant: str
) -> tuple[PurchaseOrderObservation, ...]:
    """Canonical PO observations for one material-plant.

    Unfiltered: cancelled lines and implausible durations are included, because
    which of those to exclude is Phase 3 policy and the contract carries the
    flags needed to decide.
    """
    rows = session.execute(
        select(StagedPurchaseOrder)
        .where(
            StagedPurchaseOrder.sap_material_number == material,
            StagedPurchaseOrder.sap_plant_code == plant,
        )
        .order_by(StagedPurchaseOrder.purchasing_document, StagedPurchaseOrder.item)
    ).scalars().all()

    return tuple(
        PurchaseOrderObservation(
            key=_key(row.sap_material_number, row.sap_plant_code),
            purchasing_document=row.purchasing_document,
            item=row.item,
            created_on=row.created_on,
            goods_receipt_date=row.goods_receipt_date,
            quantity_ordered=float(row.quantity_ordered) if row.quantity_ordered else None,
            quantity_received=float(row.quantity_received) if row.quantity_received else None,
            supplier=row.supplier,
            is_cancelled=row.is_cancelled,
        )
        for row in rows
        # A receipt before its order is impossible; the contract rejects it, and
        # the adapter has already recorded the rejection.
        if not (
            row.created_on
            and row.goods_receipt_date
            and row.goods_receipt_date < row.created_on
        )
    )

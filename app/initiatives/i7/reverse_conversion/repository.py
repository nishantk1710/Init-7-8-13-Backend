"""Read-only queries for the Min-Max -> OAR recommendation.

Nothing here writes, and nothing here decides: it fetches the attributes of the in-scope
material-plants and their staged monthly consumption, and the two timestamps the anchor needs. The
rule is in :mod:`~app.initiatives.i7.reverse_conversion.evaluate`.

WHERE EACH INPUT COMES FROM

* **Attributes** -- ``i7_material_feature`` (MRP type, criticality, lead time, stock, price, MINBE,
  MABST), with the description and the deletion flags from the staged material tables. The feature
  store already leaves out deletion-flagged material-plants; the flags are read again anyway so a
  flag set after the last feature build is honoured too.
* **Consumption** -- ``i7_staged_consumption``, month by month, up to the anchor. The stored
  ``consumption_count_12m`` is deliberately NOT read: it is measured to the feature build's own window
  end and so can include a partial month.
* **Load time** -- ``csv_extract_request.fired_at`` of the latest completed MSEG extract, used only when
  ``I7_REVERSE_ANCHOR_SOURCE=csv_extract``. ``fired_at`` (when SAP was asked) is chosen over the later
  ``completed_at`` / ``loaded_at`` because a month that ended before SAP was asked is certainly complete,
  whereas one that ended before the data merely arrived might not be.

Streaming: consumption is read ordered by material, plant and month and handed over one material-plant
at a time, so memory holds one series, not the table.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from itertools import groupby

from sqlalchemy import and_, exists, false, func, or_, select
from sqlalchemy.orm import Session

from app.initiatives.i7.policy.reverse_conversion import ReverseConversionPolicy
from app.initiatives.i7.reverse_conversion.types import MonthlyMovement
from app.models.csv_extract import STATUS_COMPLETE, CsvExtractRequest
from app.models.i7_features import MaterialFeature
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedConsumption, StagedMaterial, StagedMaterialPlant

#: The SAP table whose extract the consumption comes from.
CONSUMPTION_SAP_TABLE = "MSEG"

_FETCH_SIZE = 5000


@dataclass(frozen=True)
class AttributeRow:
    """One in-scope material-plant's attributes, as read."""

    material: str
    plant: str
    mrp_type: str | None
    description: str | None
    criticality: str | None
    lead_time_days: Decimal | None
    current_stock: Decimal | None
    unit_price: Decimal | None
    minbe: Decimal | None
    mabst: Decimal | None
    deletion_flag: bool | None
    """True when MARC (this plant) or MARA (client level) flags it for deletion; None when neither says."""
    currency: str | None = None
    """The currency SAP stated for ``unit_price`` (the feature store's own), or None when none was stated."""


def _deleted(*flags: bool | None) -> bool | None:
    """True if any flag says deleted; otherwise False if any says not deleted; None when none says."""
    if any(flag is True for flag in flags):
        return True
    if any(flag is False for flag in flags):
        return False
    return None


def _scope_condition(policy: ReverseConversionPolicy):
    return and_(
        MaterialFeature.mrp_type.in_(policy.mrp_types),
        MaterialFeature.sap_plant_code.in_(policy.plants),
    )


def fetch_attributes(session: Session, policy: ReverseConversionPolicy) -> list[AttributeRow]:
    """Every material-plant of the scope, once, in a stable order."""
    # Material-level facts first, in their own query: a join on the material would multiply a
    # material-plant if a material were ever staged twice.
    material_facts: dict[str, tuple[str | None, bool | None]] = {}
    in_scope_materials = exists().where(
        MaterialFeature.sap_material_number == StagedMaterial.sap_material_number,
        _scope_condition(policy),
    )
    for number, description, deleted in session.execute(
        select(StagedMaterial.sap_material_number, StagedMaterial.description, StagedMaterial.deletion_flag).where(in_scope_materials)
    ):
        known_description, known_deleted = material_facts.get(number, (None, None))
        material_facts[number] = (known_description or description, _deleted(known_deleted, deleted))

    statement = (
        select(
            MaterialFeature.sap_material_number,
            MaterialFeature.sap_plant_code,
            MaterialFeature.mrp_type,
            MaterialFeature.criticality,
            MaterialFeature.lead_time_days,
            MaterialFeature.current_stock,
            MaterialFeature.unit_price,
            MaterialFeature.currency,
            MaterialFeature.current_reorder_point,
            MaterialFeature.current_maximum_stock,
            StagedMaterialPlant.deletion_flag,
        )
        .select_from(MaterialFeature)
        .outerjoin(
            StagedMaterialPlant,
            and_(
                StagedMaterialPlant.sap_material_number == MaterialFeature.sap_material_number,
                StagedMaterialPlant.sap_plant_code == MaterialFeature.sap_plant_code,
            ),
        )
        .where(_scope_condition(policy))
        .order_by(MaterialFeature.sap_material_number, MaterialFeature.sap_plant_code)
    )
    rows: list[AttributeRow] = []
    for number, plant, mrp, criticality, lead, stock, price, currency, minbe, mabst, plant_deleted in session.execute(statement):
        description, client_deleted = material_facts.get(number, (None, None))
        rows.append(
            AttributeRow(
                material=number,
                plant=plant,
                mrp_type=mrp,
                description=description,
                criticality=criticality,
                lead_time_days=lead,
                current_stock=stock,
                unit_price=price,
                minbe=minbe,
                mabst=mabst,
                deletion_flag=_deleted(plant_deleted, client_deleted),
                currency=currency,
            )
        )
    return rows


def iter_consumption(
    session: Session, policy: ReverseConversionPolicy, anchor: date
) -> Iterator[tuple[tuple[str, str], list[MonthlyMovement]]]:
    """``((material, plant), months)`` for each material-plant of the scope that has any staged
    consumption up to the anchor, one at a time, months in order.

    Months after the anchor are never read (a partial month is excluded at the source, not corrected
    for later). A material-plant with no rows is simply absent: the caller treats that as 'never moved'.
    """
    in_scope = exists().where(
        MaterialFeature.sap_material_number == StagedConsumption.sap_material_number,
        MaterialFeature.sap_plant_code == StagedConsumption.sap_plant_code,
        _scope_condition(policy),
    )
    statement = (
        select(
            StagedConsumption.sap_material_number,
            StagedConsumption.sap_plant_code,
            StagedConsumption.period,
            StagedConsumption.quantity,
            StagedConsumption.issue_count,
            StagedConsumption.reversal_count,
        )
        .where(
            StagedConsumption.sap_plant_code.in_(policy.plants),
            StagedConsumption.period <= anchor,
            in_scope,
        )
        .order_by(
            StagedConsumption.sap_material_number,
            StagedConsumption.sap_plant_code,
            StagedConsumption.period,
        )
        .execution_options(yield_per=_FETCH_SIZE)
    )
    for key, rows in groupby(session.execute(statement), key=lambda row: (row[0], row[1])):
        yield key, [
            MonthlyMovement(period=period, net_issued_quantity=quantity, issue_count=issues, reversal_count=reversals)
            for _material, _plant, period, quantity, issues, reversals in rows
        ]


@dataclass(frozen=True)
class MinMaxAlternative:
    """What the Stock item recommendation says for a material-plant: the ROP and max stock it would keep it on Min-Max
    with. Shown beside a Convert to OAR recommendation of Medium or Low confidence, for comparison only."""

    current_rop: Decimal | None
    recommended_rop: Decimal | None
    current_max_stock: Decimal | None
    recommended_max_stock: Decimal | None


def fetch_alternatives(session: Session, keys: list[tuple[str, str]]) -> dict[tuple[str, str], MinMaxAlternative]:
    """The newest Stock item (non-OAR) recommendation of each material-plant in ``keys`` that has one.

    Read-only, and it reads the stored recommendation exactly as it is: nothing about it is recalculated or
    changed here. A material-plant with no Stock item recommendation is simply absent."""
    if not keys:
        return {}
    wanted = set(keys)
    statement = (
        select(
            Recommendation.sap_material_number,
            Recommendation.sap_plant_code,
            Recommendation.current_rop,
            Recommendation.recommended_rop,
            Recommendation.current_max_stock,
            Recommendation.recommended_max_stock,
        )
        .where(
            Recommendation.sap_material_number.in_(sorted({material for material, _ in keys})),
            or_(Recommendation.is_oar.is_(None), Recommendation.is_oar == false()),
        )
        .order_by(Recommendation.id)
    )
    newest: dict[tuple[str, str], MinMaxAlternative] = {}
    for material, plant, current_rop, recommended_rop, current_max, recommended_max in session.execute(statement):
        if (material, plant) in wanted:
            newest[(material, plant)] = MinMaxAlternative(current_rop, recommended_rop, current_max, recommended_max)  # ascending ids: the last wins
    return newest


def fetch_material_consumption(
    session: Session, material: str, plant: str, anchor: date, first_month: date
) -> list[MonthlyMovement]:
    """One material-plant's staged months from ``first_month`` to the anchor, in order. Read-only; months
    after the anchor are never read, as in :func:`iter_consumption`."""
    statement = (
        select(
            StagedConsumption.period,
            StagedConsumption.quantity,
            StagedConsumption.issue_count,
            StagedConsumption.reversal_count,
        )
        .where(
            StagedConsumption.sap_material_number == material,
            StagedConsumption.sap_plant_code == plant,
            StagedConsumption.period >= first_month,
            StagedConsumption.period <= anchor,
        )
        .order_by(StagedConsumption.period)
    )
    return [
        MonthlyMovement(period=period, net_issued_quantity=quantity, issue_count=issues, reversal_count=reversals)
        for period, quantity, issues, reversals in session.execute(statement)
    ]


def latest_staged_month(session: Session) -> date | None:
    """The latest month any consumption is staged for (all plants), or None."""
    return session.execute(select(func.max(StagedConsumption.period))).scalar_one_or_none()


def latest_extract_request_time(session: Session) -> datetime | None:
    """When SAP was last asked for a completed, loaded MSEG extract (``fired_at``), or None."""
    return session.execute(
        select(func.max(CsvExtractRequest.fired_at)).where(
            CsvExtractRequest.sap_table == CONSUMPTION_SAP_TABLE,
            CsvExtractRequest.status == STATUS_COMPLETE,
            CsvExtractRequest.loaded_at.is_not(None),
        )
    ).scalar_one_or_none()

"""OAR candidate population and inventory lookups.

The only module in Phase 6 that reads the staging and feature-store tables --
everything above it works with :class:`CandidateAttributes` and never learns a
column name.

**The candidate universe is the classified population, not the whole
catalogue.** A neighbour must itself have usable history (Solution Design:
">= 12 months"), so only material-plants Phase 3 actually classified
(SMOOTH/ERRATIC/INTERMITTENT/LUMPY) are candidates. The other 44,938
material-plants are cold-start targets, not donors, and querying against them
would be work spent on rows that can never pass eligibility.

**And only those with something to lend.** A donor must also have its current
SAP ROP, safety stock and max stock all above zero, and those SAP values are
what it lends -- not I07's own Phase 5 calculation.
"""

from decimal import ROUND_CEILING, Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.initiatives.i7.oar.business_similarity import log_price
from app.initiatives.i7.oar.types import CandidateAttributes

_TARGET_SQL = """
    SELECT f.sap_material_number, f.sap_plant_code, f.criticality,
           m.deletion_flag, f.total_periods,
           m.material_group, NULL AS equipment_type, m.base_unit_of_measure,
           NULL AS circuit, m.manufacturer, f.unit_price, m.description,
           m.material_type
      FROM i7_material_feature f
      LEFT JOIN i7_staged_material m ON m.sap_material_number = f.sap_material_number
     WHERE f.oar_scope = 'IN_SCOPE'
"""
# Every OAR material-plant (MRP type ND / PD / blank, Stage 1's verdict), whatever its history and
# demand class. A VB / other material is never a target: it is forecast or not evaluable.

_DONOR_WHERE = """
     WHERE f.history_status = 'SUFFICIENT'
       AND f.current_safety_stock > 0
       AND f.current_reorder_point > 0
       AND f.current_maximum_stock > 0
"""
# A neighbour must have all three current SAP values (MARC EISBE / MINBE / MABST) set and above
# zero: they are what it lends. A 0 in SAP usually means "never set up", and averaging it in would
# drag every estimate down. The candidate population and the values it lends share this one
# condition, so no candidate can exist that has nothing to lend.

_CANDIDATE_SQL = (
    """
    SELECT f.sap_material_number, f.sap_plant_code, f.criticality,
           m.deletion_flag, f.total_periods,
           m.material_group, NULL AS equipment_type, m.base_unit_of_measure,
           NULL AS circuit, m.manufacturer, f.unit_price, m.description,
           m.material_type
      FROM i7_material_feature f
      LEFT JOIN i7_staged_material m ON m.sap_material_number = f.sap_material_number
"""
    + _DONOR_WHERE
)
# unit_price reads from the feature store (f.), not re-joined from
# i7_staged_material (m.): the Phase 3 builder already carried it through from
# staging, so this was a second, independent read of the same value.

_DONOR_VALUES_SQL = (
    """
    SELECT f.sap_material_number, f.sap_plant_code,
           f.current_safety_stock, f.current_reorder_point, f.current_maximum_stock
      FROM i7_material_feature f
"""
    + _DONOR_WHERE
)


def _to_attributes(row) -> CandidateAttributes:
    """A staged row to the canonical similarity contract.

    ``deletion_flag`` is a resolved boolean throughout staging (Phase 2's
    ``parse_flag`` never returns ``None``), so active status is always known on
    this extract -- ``is_active`` stays typed as optional because a future
    source could genuinely leave it unknown, not because this one does.
    """
    return CandidateAttributes(
        sap_material_number=row.sap_material_number,
        sap_plant_code=row.sap_plant_code,
        criticality=row.criticality,
        is_active=(not row.deletion_flag) if row.deletion_flag is not None else None,
        history_months=row.total_periods,
        material_group=row.material_group,
        equipment_type=row.equipment_type,
        base_unit_of_measure=row.base_unit_of_measure,
        circuit=row.circuit,
        manufacturer=row.manufacturer,
        unit_price=Decimal(str(row.unit_price)) if row.unit_price is not None else None,
        description=row.description,
        material_type=row.material_type,
    )


def load_targets(session: Session) -> list[CandidateAttributes]:
    """Every OAR material-plant (MRP type ND / PD / blank), whatever its history."""
    return [_to_attributes(row) for row in session.execute(text(_TARGET_SQL))]


def load_targets_for(session: Session, keys: list[tuple[str, str]]) -> list[CandidateAttributes]:
    """The OAR targets among these ``(material, plant)`` keys -- the same rows and the same shape
    ``load_targets`` returns, for a refresh of one or a few targets rather than the whole population."""
    found: list[CandidateAttributes] = []
    for material, plant in keys:
        found.extend(
            _to_attributes(row)
            for row in session.execute(
                text(_TARGET_SQL + " AND f.sap_material_number = :material AND f.sap_plant_code = :plant"),
                {"material": material, "plant": plant},
            )
        )
    return found


def load_candidate_population(session: Session) -> list[CandidateAttributes]:
    """Every classified material-plant (Phase 3's SUFFICIENT history status)
    whose current SAP ROP, safety stock and max stock are all above zero -- the
    only population eligible to be a neighbour. Similarity, history, criticality
    and active-status gates are applied later, per target."""
    return [_to_attributes(row) for row in session.execute(text(_CANDIDATE_SQL))]


PRICE_BAND_LOW_PERCENT = 5
PRICE_BAND_HIGH_PERCENT = 95


def price_range(candidates: list[CandidateAttributes]) -> Decimal | None:
    """The population's spread of ln(price) (5th-95th percentile), for the numeric Gower distance.

    Computed once over the whole candidate population rather than per pair --
    a range derived from two values would make every pair maximally distant.
    """
    logs = sorted(
        value for value in (log_price(c.unit_price) for c in candidates) if value is not None
    )
    if len(logs) < 2:
        return None
    # The 5th-95th percentile of ln(price), not min-max: a few repairables costing millions made the
    # linear range so wide that every ordinary item looked like the same price (99.99% alike)
    # and the price term separated nothing. Distances beyond the band are clamped to 1.
    low = logs[(PRICE_BAND_LOW_PERCENT * (len(logs) - 1)) // 100]
    high = logs[round(PRICE_BAND_HIGH_PERCENT * (len(logs) - 1) / 100)]
    span = high - low
    if span <= 0:
        span = logs[-1] - logs[0]
    return span if span > 0 else None


def _whole_units(value) -> int:
    """SAP quantity to the whole units a neighbour value is stored in, rounded
    up so a fractional value above zero never lends 0."""
    return int(Decimal(str(value)).to_integral_value(rounding=ROUND_CEILING))


def load_inventory_values(session: Session) -> dict[tuple[str, str], dict]:
    """The ROP, safety stock and max stock each neighbour lends, keyed by material-plant.

    Always the neighbour's **current SAP values** (MARC), whatever its demand
    class -- never I07's own Phase 5 calculation, even where one succeeded.
    Covers exactly the candidate population (same condition), so every
    neighbour has something to lend.
    """
    return {
        (row.sap_material_number, row.sap_plant_code): {
            "safety_stock": _whole_units(row.current_safety_stock),
            "rop": _whole_units(row.current_reorder_point),
            "max_stock": _whole_units(row.current_maximum_stock),
        }
        for row in session.execute(text(_DONOR_VALUES_SQL))
    }

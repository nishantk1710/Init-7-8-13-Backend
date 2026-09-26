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
"""

from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.initiatives.i7.oar.types import CandidateAttributes

_TARGET_SQL = """
    SELECT f.sap_material_number, f.sap_plant_code, f.criticality,
           m.deletion_flag, f.total_periods,
           m.material_group, NULL AS equipment_type, m.base_unit_of_measure,
           NULL AS circuit, m.manufacturer, f.unit_price, m.description
      FROM i7_material_feature f
      LEFT JOIN i7_staged_material m ON m.sap_material_number = f.sap_material_number
     WHERE f.history_status <> 'SUFFICIENT'
"""

_CANDIDATE_SQL = """
    SELECT f.sap_material_number, f.sap_plant_code, f.criticality,
           m.deletion_flag, f.total_periods,
           m.material_group, NULL AS equipment_type, m.base_unit_of_measure,
           NULL AS circuit, m.manufacturer, f.unit_price, m.description
      FROM i7_material_feature f
      LEFT JOIN i7_staged_material m ON m.sap_material_number = f.sap_material_number
     WHERE f.history_status = 'SUFFICIENT'
"""
# unit_price reads from the feature store (f.), not re-joined from
# i7_staged_material (m.): the Phase 3 builder already carried it through from
# staging, so this was a second, independent read of the same value.

_INVENTORY_SQL = """
    SELECT sap_material_number, sap_plant_code,
           safety_stock_status, safety_stock,
           rop_status, rop,
           max_stock_status, max_stock
      FROM i7_inventory_calculation
     WHERE inventory_run_id = :inventory_run_id
"""


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
    )


def load_targets(session: Session) -> list[CandidateAttributes]:
    """Every cold-start material-plant (NO_HISTORY or COLD_START)."""
    return [_to_attributes(row) for row in session.execute(text(_TARGET_SQL))]


def load_candidate_population(session: Session) -> list[CandidateAttributes]:
    """Every classified material-plant -- the only population eligible to be a
    neighbour (Phase 3's SUFFICIENT history status)."""
    return [_to_attributes(row) for row in session.execute(text(_CANDIDATE_SQL))]


def price_range(candidates: list[CandidateAttributes]) -> Decimal | None:
    """The population's price span, for the numeric Gower distance.

    Computed once over the whole candidate population rather than per pair --
    a range derived from two values would make every pair maximally distant.
    """
    prices = [c.unit_price for c in candidates if c.unit_price is not None]
    if len(prices) < 2:
        return None
    span = max(prices) - min(prices)
    return span if span > 0 else None


def load_inventory_values(
    session: Session, inventory_run_id: int | None
) -> dict[tuple[str, str], dict]:
    """Phase 5 SUCCESS values, keyed by material-plant.

    Only materials whose own calculation succeeded appear here. A neighbour
    absent from this map is a valid neighbour with nothing to lend -- similarity
    eligibility and inventory eligibility are answered separately.
    """
    if inventory_run_id is None:
        return {}

    values: dict[tuple[str, str], dict] = {}
    for row in session.execute(
        text(_INVENTORY_SQL), {"inventory_run_id": inventory_run_id}
    ):
        if (
            row.safety_stock_status == "SUCCESS"
            and row.rop_status == "SUCCESS"
            and row.max_stock_status == "SUCCESS"
        ):
            values[(row.sap_material_number, row.sap_plant_code)] = {
                "safety_stock": row.safety_stock,
                "rop": row.rop,
                "max_stock": row.max_stock,
            }
    return values

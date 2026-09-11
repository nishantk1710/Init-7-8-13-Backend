"""OAR -> Min-Max reclassification evidence.

This is evidence only -- I13 never changes the SAP MRP type and never calls
an I7 recommendation engine. The result is a plain DTO I7 may later consume
(``ReclassificationCandidate`` -> API -> I7), which is the only allowed
direction across that boundary.
"""

from datetime import date

from app.initiatives.i13.aging import compute_aging, group_by_material_plant
from app.initiatives.i13.config import I13Config
from app.initiatives.i13.models import ReclassificationCandidate
from app.integrations.sap.gateway import SapGateway
from app.shared.material_scope import MaterialScope, classify_material_scope


def build_reclassification_candidates(
    gateway: SapGateway, config: I13Config, *, as_of: date | None = None
) -> list[ReclassificationCandidate]:
    as_of = as_of or date.today()

    material_plants = gateway.get_material_plants().rows
    movements = gateway.get_goods_movements().rows
    movements_by_key = group_by_material_plant(movements)

    candidates: list[ReclassificationCandidate] = []
    for row in material_plants:
        material, plant = row.get("Matnr"), row.get("Werks")
        if material is None or plant is None:
            continue
        if classify_material_scope(row.get("Dismm")) is not MaterialScope.OAR:
            continue

        aging = compute_aging(
            material,
            plant,
            movements_by_key.get((material, plant), []),
            current_stock=None,
            thresholds=config.aging,
            window_months=config.watch.consumption_window_months,
            as_of=as_of,
        )

        consumed_more_than_threshold = aging.consumption_count_12m > config.reclassification.min_consumption_count
        reasons: list[str] = []
        if consumed_more_than_threshold:
            reasons.append(
                f"Consumed {aging.consumption_count_12m} times in trailing 12 months "
                f"(> {config.reclassification.min_consumption_count})"
            )

        candidates.append(
            ReclassificationCandidate(
                material=material,
                plant=plant,
                consumption_count_12m=aging.consumption_count_12m,
                consumed_more_than_threshold=consumed_more_than_threshold,
                critical_impact_indicator=None,
                hod_justified_request_indicator=None,
                data_available=False,
                candidate_flag=consumed_more_than_threshold,
                candidate_reasons=reasons,
            )
        )
    return candidates

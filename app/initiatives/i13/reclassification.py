"""OAR -> Min-Max reclassification evidence.

This is evidence only -- I13 never changes the SAP MRP type and never calls
an I7 recommendation engine. The result is a plain DTO I7 may later consume
(``ReclassificationCandidate`` -> API -> I7), which is the only allowed
direction across that boundary.

Postgres-backed: OAR scope from ``raw_marc`` (``postgres_material.py``),
consumption evidence from real goods-movement history
(``movement_metrics.py``, W3.5) -- no CSV, no SapGateway.
"""

from datetime import date

from app.initiatives.i13.config import I13Config
from app.initiatives.i13.models import ReclassificationCandidate
from app.initiatives.i13.movement_metrics import compute_all_movement_metrics
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.shared.material_scope import MaterialScope, classify_material_scope


def build_reclassification_candidates(
    movement_repository: PostgresMovementRepository,
    material_scope_index: dict[tuple[str, str], str | None],
    config: I13Config,
    *,
    material: str | None = None,
    plant: str | None = None,
    as_of: date | None = None,
) -> list[ReclassificationCandidate]:
    metrics_by_key = {
        (metric.material, metric.plant): metric
        for metric in compute_all_movement_metrics(
            movement_repository,
            thresholds=config.aging,
            window_months=config.watch.consumption_window_months,
            as_of=as_of,
            material=material,
            plant=plant,
        )
    }

    candidates: list[ReclassificationCandidate] = []
    for key, dismm in material_scope_index.items():
        if material and key[0] != material:
            continue
        if plant and key[1] != plant:
            continue
        if classify_material_scope(dismm) is not MaterialScope.OAR:
            continue

        # An OAR material with no movement history at all is a legitimate
        # zero-consumption candidate, not an omission -- unlike
        # compute_all_movement_metrics (which only returns keys it has
        # movement rows for), every OAR (material, plant) must appear here.
        metric = metrics_by_key.get(key)
        consumption_count_12m = metric.consumption_count_12m if metric else 0

        consumed_more_than_threshold = consumption_count_12m > config.reclassification.min_consumption_count
        reasons: list[str] = []
        if consumed_more_than_threshold:
            reasons.append(
                f"Consumed {consumption_count_12m} times in trailing 12 months "
                f"(> {config.reclassification.min_consumption_count})"
            )

        candidates.append(
            ReclassificationCandidate(
                material=key[0],
                plant=key[1],
                consumption_count_12m=consumption_count_12m,
                consumed_more_than_threshold=consumed_more_than_threshold,
                critical_impact_indicator=None,
                hod_justified_request_indicator=None,
                data_available=False,
                candidate_flag=consumed_more_than_threshold,
                candidate_reasons=reasons,
            )
        )
    return candidates

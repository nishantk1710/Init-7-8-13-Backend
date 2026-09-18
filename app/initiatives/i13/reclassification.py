"""OAR -> Min-Max reclassification evidence (SOP 3.1.1, W6.5).

This is evidence only -- I13 never changes the SAP MRP type, never computes
ROP/Max, and never calls an I7 recommendation engine. The result is a plain
DTO I7 may later consume (``ReclassificationCandidate`` -> API -> I7), which
is the only allowed direction across that boundary.

Three SOP 3.1.1 indicators, OR'd together:

1. FREQUENT_CONSUMPTION -- consumed more than ``config.reclassification.
   min_consumption_count`` times in the trailing 12 months (W3.5, via
   ``movement_metrics.py`` -- never a second goods-movement/reversal rule
   computed here).
2. CRITICAL -- the material-plant's criticality tier (W3.4, via the shared
   ``CriticalitySource`` port) is one of ``config.reclassification.
   critical_tiers``.
3. HOD_JUSTIFIED -- an HOD-approved justification request exists (via
   ``HodJustificationProvider`` -- no W6.6 workflow is built yet, so this is
   always UNKNOWN today; see that module's docstring).

A missing indicator source (criticality tier not found, HOD provider
unavailable) is UNKNOWN, never fabricated as ``False``: an OAR material can
still be flagged a candidate on the indicators that ARE available, but
``data_available`` stays ``False`` so a consumer never mistakes a partial
answer for proven negative evidence on all three indicators.

Postgres-backed OAR scope (``raw_marc`` via ``postgres_material.py``) and
consumption evidence (real goods-movement history, W3.5) -- no CSV, no
SapGateway.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.initiatives.i13.config import I13Config
from app.initiatives.i13.hod_justification_provider import (
    HodJustificationProvider,
    NullHodJustificationProvider,
)
from app.initiatives.i13.models import ReclassificationCandidate, ReclassificationReason
from app.initiatives.i13.movement_metrics import compute_all_movement_metrics
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.shared import CriticalitySource, get_criticality_source
from app.shared.material_scope import MaterialScope, classify_material_scope


def build_reclassification_candidates(
    movement_repository: PostgresMovementRepository,
    material_scope_index: dict[tuple[str, str], str | None],
    config: I13Config,
    *,
    material: str | None = None,
    plant: str | None = None,
    as_of: date | None = None,
    criticality_source: CriticalitySource | None = None,
    hod_provider: HodJustificationProvider | None = None,
) -> list[ReclassificationCandidate]:
    as_of = as_of or date.today()
    generated_at = datetime.now(timezone.utc)
    criticality_source = criticality_source or get_criticality_source()
    hod_provider = hod_provider or NullHodJustificationProvider()

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

        criticality_result = criticality_source.get(key[0], key[1])
        critical_impact_indicator = (
            criticality_result.tier in config.reclassification.critical_tiers
            if criticality_result.found
            else None
        )

        hod_justified_request_indicator = hod_provider.get_hod_justification(material=key[0], plant=key[1])

        reasons: list[str] = []
        if consumed_more_than_threshold:
            reasons.append(ReclassificationReason.FREQUENT_CONSUMPTION.value)
        if critical_impact_indicator:
            reasons.append(ReclassificationReason.CRITICAL.value)
        if hod_justified_request_indicator:
            reasons.append(ReclassificationReason.HOD_JUSTIFIED.value)

        data_available = critical_impact_indicator is not None and hod_justified_request_indicator is not None

        candidates.append(
            ReclassificationCandidate(
                material=key[0],
                plant=key[1],
                as_of_date=as_of,
                consumption_count_12m=consumption_count_12m,
                consumption_threshold=config.reclassification.min_consumption_count,
                consumed_more_than_threshold=consumed_more_than_threshold,
                critical_impact_indicator=critical_impact_indicator,
                hod_justified_request_indicator=hod_justified_request_indicator,
                data_available=data_available,
                candidate_flag=bool(reasons),
                generated_at=generated_at,
                candidate_reasons=reasons,
            )
        )
    return candidates

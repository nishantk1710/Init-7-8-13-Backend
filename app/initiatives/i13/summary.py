"""Dashboard-ready I13 summary aggregates.

Computed here, not in the API controller -- ``app/api/i13/routes.py`` only
serializes the result.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.initiatives.i13.aging import compute_aging, group_by_material_plant
from app.initiatives.i13.config import I13Config
from app.initiatives.i13.exceptions import build_exception_queue
from app.initiatives.i13.models import AgingBand, ExceptionType
from app.initiatives.i13.reclassification import build_reclassification_candidates
from app.integrations.sap.gateway import SapGateway
from app.shared.material_scope import MaterialScope, classify_material_scope


@dataclass(frozen=True)
class I13Summary:
    total_oar_positions: int
    fast_moving_count: int
    slow_moving_count: int
    non_moving_count: int
    gr_not_issued_30_day_count: int
    plan_breach_count: int
    no_plan_count: int
    reclassification_candidate_count: int
    valuation_is_mocked: bool


def build_summary(gateway: SapGateway, config: I13Config, data_dir: Path, *, as_of: date | None = None) -> I13Summary:
    as_of = as_of or date.today()

    material_plants = gateway.get_material_plants().rows
    oar_positions = [row for row in material_plants if classify_material_scope(row.get("Dismm")) is MaterialScope.OAR]

    movements = gateway.get_goods_movements().rows
    movements_by_key = group_by_material_plant(movements)

    band_counts = {AgingBand.FAST: 0, AgingBand.SLOW: 0, AgingBand.NON_MOVING: 0}
    for row in oar_positions:
        material, plant = row.get("Matnr"), row.get("Werks")
        aging = compute_aging(
            material,
            plant,
            movements_by_key.get((material, plant), []),
            current_stock=None,
            thresholds=config.aging,
            window_months=config.watch.consumption_window_months,
            as_of=as_of,
        )
        if aging.aging_band in band_counts:
            band_counts[aging.aging_band] += 1

    exceptions = build_exception_queue(gateway, config, data_dir, as_of=as_of)
    exception_counts = {exception_type: 0 for exception_type in ExceptionType}
    for exception in exceptions:
        exception_counts[exception.type] += 1

    reclassification_candidates = build_reclassification_candidates(gateway, config, as_of=as_of)
    candidate_count = sum(1 for candidate in reclassification_candidates if candidate.candidate_flag)

    return I13Summary(
        total_oar_positions=len(oar_positions),
        fast_moving_count=band_counts[AgingBand.FAST],
        slow_moving_count=band_counts[AgingBand.SLOW],
        non_moving_count=band_counts[AgingBand.NON_MOVING],
        gr_not_issued_30_day_count=exception_counts[ExceptionType.GR_NOT_ISSUED_30_DAY],
        plan_breach_count=exception_counts[ExceptionType.PLAN_BREACH],
        no_plan_count=exception_counts[ExceptionType.NO_PLAN],
        reclassification_candidate_count=candidate_count,
        valuation_is_mocked=True,
    )

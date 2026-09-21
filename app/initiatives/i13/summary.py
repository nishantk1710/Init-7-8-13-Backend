"""Dashboard-ready I13 summary aggregates.

Computed here, not in the API controller -- ``app/api/i13/routes.py`` only
serializes the result.

Postgres-backed: composes ``movement_metrics.py`` (W3.5), ``exceptions.py``
and ``reclassification.py`` rather than reading a gateway/CSV directly.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.initiatives.i13.config import I13Config
from app.initiatives.i13.exceptions import build_exception_queue
from app.initiatives.i13.models import AgingBand, ExceptionType
from app.initiatives.i13.movement_metrics import compute_all_movement_metrics
from app.initiatives.i13.reclassification import build_reclassification_candidates
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
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


def build_summary(
    movement_repository: PostgresMovementRepository,
    procurement_repository: PostgresProcurementRepository,
    reservation_repository: PostgresReservationRepository,
    material_scope_index: dict[tuple[str, str], str | None],
    config: I13Config,
    data_dir: Path,
    *,
    as_of: date | None = None,
) -> I13Summary:
    as_of = as_of or date.today()

    oar_keys = {key for key, dismm in material_scope_index.items() if classify_material_scope(dismm) is MaterialScope.OAR}

    metrics = compute_all_movement_metrics(
        movement_repository, thresholds=config.aging, window_months=config.watch.consumption_window_months, as_of=as_of
    )
    band_counts = {AgingBand.FAST: 0, AgingBand.SLOW: 0, AgingBand.NON_MOVING: 0}
    oar_keys_seen: set[tuple[str, str]] = set()
    for metric in metrics:
        key = (metric.material, metric.plant)
        if key not in oar_keys:
            continue
        oar_keys_seen.add(key)
        band_counts[metric.aging_band] += 1
    # An OAR material with zero movement history never appears in ``metrics``
    # (compute_all_movement_metrics only returns keys with movement rows) --
    # it is still a real OAR position, and its aging band is NON_MOVING by
    # definition (no last-issue date at all).
    band_counts[AgingBand.NON_MOVING] += len(oar_keys - oar_keys_seen)

    exceptions = build_exception_queue(
        movement_repository, procurement_repository, reservation_repository, material_scope_index, config, data_dir, as_of=as_of
    )
    exception_counts = {exception_type: 0 for exception_type in ExceptionType}
    for exception in exceptions:
        exception_counts[exception.type] += 1

    reclassification_candidates = build_reclassification_candidates(
        movement_repository, material_scope_index, config, as_of=as_of
    )
    candidate_count = sum(1 for candidate in reclassification_candidates if candidate.candidate_flag)

    return I13Summary(
        total_oar_positions=len(oar_keys),
        fast_moving_count=band_counts[AgingBand.FAST],
        slow_moving_count=band_counts[AgingBand.SLOW],
        non_moving_count=band_counts[AgingBand.NON_MOVING],
        gr_not_issued_30_day_count=exception_counts[ExceptionType.GR_NOT_ISSUED_30_DAY],
        plan_breach_count=exception_counts[ExceptionType.PLAN_BREACH],
        no_plan_count=exception_counts[ExceptionType.NO_PLAN],
        reclassification_candidate_count=candidate_count,
        valuation_is_mocked=True,
    )

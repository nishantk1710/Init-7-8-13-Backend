"""GET /api/i13/movement-metrics, GET /api/i13/materials/{material}/plants/{plant}/movement-metrics.

W3.5. Reads real Postgres goods-movement data only (``PostgresMovementRepository``)
-- there is no CSV/mock fallback here, unlike the rest of I13's still-CSV-backed
routes. Routes stay thin: all computation lives in
``app.initiatives.i13.movement_metrics``.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.i13.deps import page, snapshot_or_live
from app.core.db import get_db
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.movement_metrics import compute_all_movement_metrics
from app.initiatives.i13.snapshot import I13Snapshot
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.schemas.i13 import MovementMetricsResponse

router = APIRouter()


@router.get("/movement-metrics", response_model=list[MovementMetricsResponse])
def list_movement_metrics(
    response: Response,
    plant: str | None = Query(None),
    material: str | None = Query(None),
    aging_band: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
    snapshot: I13Snapshot | None = Depends(snapshot_or_live),
) -> list[MovementMetricsResponse]:
    if snapshot is not None:
        metrics = [
            m
            for m in snapshot.movement_metrics
            if (not plant or m.plant == plant) and (not material or m.material == material)
        ]
    else:
        repository = PostgresMovementRepository(db)
        metrics = compute_all_movement_metrics(
            repository,
            thresholds=config.aging,
            window_months=config.watch.consumption_window_months,
            material=material,
            plant=plant,
        )
    if aging_band:
        metrics = [metric for metric in metrics if metric.aging_band.value == aging_band.upper()]
    return [MovementMetricsResponse.model_validate(metric) for metric in page(metrics, response, limit=limit, offset=offset)]


@router.get(
    "/materials/{material}/plants/{plant}/movement-metrics",
    response_model=MovementMetricsResponse,
)
def get_movement_metrics(
    material: str,
    plant: str,
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
    snapshot: I13Snapshot | None = Depends(snapshot_or_live),
) -> MovementMetricsResponse:
    if snapshot is not None:
        metrics = [m for m in snapshot.movement_metrics if m.material == material and m.plant == plant]
    else:
        repository = PostgresMovementRepository(db)
        metrics = compute_all_movement_metrics(
            repository,
            thresholds=config.aging,
            window_months=config.watch.consumption_window_months,
            material=material,
            plant=plant,
        )
    if not metrics:
        raise HTTPException(status_code=404, detail="No movement history for this material/plant")
    return MovementMetricsResponse.model_validate(metrics[0])

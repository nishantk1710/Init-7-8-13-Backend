"""GET /api/i13/movement-metrics, GET /api/i13/materials/{material}/plants/{plant}/movement-metrics.

W3.5. Reads real Postgres goods-movement data only (``PostgresMovementRepository``)
-- there is no CSV/mock fallback here, unlike the rest of I13's still-CSV-backed
routes. Routes stay thin: all computation lives in
``app.initiatives.i13.movement_metrics``.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.movement_metrics import compute_all_movement_metrics
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.schemas.i13 import MovementMetricsResponse

router = APIRouter()


@router.get("/movement-metrics", response_model=list[MovementMetricsResponse])
def list_movement_metrics(
    plant: str | None = Query(None),
    material: str | None = Query(None),
    aging_band: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
) -> list[MovementMetricsResponse]:
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
    page = metrics[offset : offset + limit]
    return [MovementMetricsResponse.model_validate(metric) for metric in page]


@router.get(
    "/materials/{material}/plants/{plant}/movement-metrics",
    response_model=MovementMetricsResponse,
)
def get_movement_metrics(
    material: str,
    plant: str,
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
) -> MovementMetricsResponse:
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

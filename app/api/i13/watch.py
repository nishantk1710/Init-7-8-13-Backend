"""GET /api/i13/watch."""

from pathlib import Path

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.i13.deps import get_data_dir, page, snapshot_or_live
from app.core.db import get_db
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.snapshot import I13Snapshot
from app.initiatives.i13.watch import compute_watch_metrics
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.schemas.i13 import WatchMetricResponse

router = APIRouter()


@router.get("/watch", response_model=list[WatchMetricResponse])
def list_watch_metrics(
    response: Response,
    plant: str | None = Query(None),
    material: str | None = Query(None),
    aging_band: str | None = Query(None),
    limit: int | None = Query(None, ge=1, le=50000, description="Unbounded when omitted, as before."),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
    data_dir: Path = Depends(get_data_dir),
    snapshot: I13Snapshot | None = Depends(snapshot_or_live),
) -> list[WatchMetricResponse]:
    if snapshot is not None:
        metrics = [
            m
            for m in snapshot.watch_sorted
            if (not plant or m.plant == plant) and (not material or m.material == material)
        ]
    else:
        movement_repo = PostgresMovementRepository(db)
        procurement_repo = PostgresProcurementRepository(db)
        reservation_repo = PostgresReservationRepository(db)
        material_scope_index = fetch_material_scope_index(db, material=material, plant=plant)
        metrics = compute_watch_metrics(
            movement_repo, procurement_repo, reservation_repo, material_scope_index, config, data_dir,
            material=material, plant=plant,
            db=db,
        )
    if aging_band:
        metrics = [metric for metric in metrics if metric.aging_band.value == aging_band.upper()]
    return [WatchMetricResponse.model_validate(metric) for metric in page(metrics, response, limit=limit, offset=offset)]

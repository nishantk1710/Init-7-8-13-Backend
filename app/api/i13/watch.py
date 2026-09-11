"""GET /api/i13/watch."""

from pathlib import Path

from fastapi import APIRouter, Depends, Query

from app.api.i13.deps import get_data_dir
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.watch import compute_watch_metrics
from app.integrations.sap.gateway import SapGateway, get_sap_gateway
from app.schemas.i13 import WatchMetricResponse

router = APIRouter()


@router.get("/watch", response_model=list[WatchMetricResponse])
def list_watch_metrics(
    plant: str | None = Query(None),
    material: str | None = Query(None),
    aging_band: str | None = Query(None),
    gateway: SapGateway = Depends(get_sap_gateway),
    config: I13Config = Depends(get_i13_config),
    data_dir: Path = Depends(get_data_dir),
) -> list[WatchMetricResponse]:
    metrics = compute_watch_metrics(gateway, config, data_dir)
    if plant:
        metrics = [metric for metric in metrics if metric.plant == plant]
    if material:
        metrics = [metric for metric in metrics if metric.material == material]
    if aging_band:
        metrics = [metric for metric in metrics if metric.aging_band.value == aging_band.upper()]
    return [WatchMetricResponse.model_validate(metric) for metric in metrics]

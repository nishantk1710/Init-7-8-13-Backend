"""Initiative 13 API: summary, data-sources, and the sub-routers below.

Mounted under ``/api/i13`` from ``app/api/router.py``. Routes stay thin --
all computation lives in ``app.initiatives.i13``.
"""

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends

from app.api.i13 import exceptions as exceptions_routes
from app.api.i13 import ledger as ledger_routes
from app.api.i13 import reclassification as reclassification_routes
from app.api.i13 import validation as validation_routes
from app.api.i13 import watch as watch_routes
from app.api.i13.deps import get_data_dir
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.summary import build_summary
from app.integrations.sap.gateway import SapGateway, get_sap_gateway
from app.schemas.i13 import DataSourceStatusResponse, I13SummaryResponse

router = APIRouter(prefix="/i13", tags=["i13"])

router.include_router(ledger_routes.router)
router.include_router(watch_routes.router)
router.include_router(exceptions_routes.router)
router.include_router(reclassification_routes.router)
router.include_router(validation_routes.router)


@router.get("/summary", response_model=I13SummaryResponse)
def get_summary(
    gateway: SapGateway = Depends(get_sap_gateway),
    config: I13Config = Depends(get_i13_config),
    data_dir: Path = Depends(get_data_dir),
) -> I13SummaryResponse:
    summary = build_summary(gateway, config, data_dir)
    return I13SummaryResponse(**asdict(summary))


@router.get("/data-sources", response_model=list[DataSourceStatusResponse])
def get_data_sources(gateway: SapGateway = Depends(get_sap_gateway)) -> list[DataSourceStatusResponse]:
    return [DataSourceStatusResponse.model_validate(status) for status in gateway.data_source_statuses()]

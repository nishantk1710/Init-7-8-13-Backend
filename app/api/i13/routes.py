"""Initiative 13 API: summary, data-sources, and the sub-routers below.

Mounted under ``/api/i13`` from ``app/api/router.py``. Routes stay thin --
all computation lives in ``app.initiatives.i13``.
"""

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.i13 import act as act_routes
from app.api.i13 import consumption_attribution as consumption_attribution_routes
from app.api.i13 import exceptions as exceptions_routes
from app.api.i13 import ledger as ledger_routes
from app.api.i13 import movement_metrics as movement_metrics_routes
from app.api.i13 import procurement_chain as procurement_chain_routes
from app.api.i13 import reclassification as reclassification_routes
from app.api.i13 import reservation_ledger as reservation_ledger_routes
from app.api.i13 import validation as validation_routes
from app.api.i13 import watch as watch_routes
from app.api.i13.deps import get_data_dir
from app.core.db import get_db
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.summary import build_summary
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.models import IngestionRun
from app.schemas.i13 import DataSourceStatusResponse, I13SummaryResponse

router = APIRouter(prefix="/i13", tags=["i13"])

router.include_router(ledger_routes.router)
router.include_router(movement_metrics_routes.router)
# procurement_chain_routes ("/utilisation-ledger/partial", ".../diagnostics")
# MUST be registered before reservation_ledger_routes
# ("/utilisation-ledger/{reservation_number}/{reservation_item}") -- FastAPI
# matches routes in registration order, and "partial/diagnostics" would
# otherwise structurally match the two-segment parameterised route first.
router.include_router(procurement_chain_routes.router)
router.include_router(reservation_ledger_routes.router)
router.include_router(consumption_attribution_routes.router)
router.include_router(watch_routes.router)
router.include_router(exceptions_routes.router)
router.include_router(reclassification_routes.router)
router.include_router(validation_routes.router)
router.include_router(act_routes.router)

# The raw extract tables I13 actually reads -- see app/seed/manifest.py for
# the full delivery; this is the I13-relevant subset.
_I13_TABLES = ("raw_eban", "raw_ekpo", "raw_ekbe", "raw_mseg", "raw_mkpf", "raw_mard", "raw_resb", "raw_marc")


@router.get("/summary", response_model=I13SummaryResponse)
def get_summary(
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
    data_dir: Path = Depends(get_data_dir),
) -> I13SummaryResponse:
    movement_repo = PostgresMovementRepository(db)
    procurement_repo = PostgresProcurementRepository(db)
    reservation_repo = PostgresReservationRepository(db)
    material_scope_index = fetch_material_scope_index(db)

    summary = build_summary(movement_repo, procurement_repo, reservation_repo, material_scope_index, config, data_dir)
    return I13SummaryResponse(**asdict(summary))


@router.get("/data-sources", response_model=list[DataSourceStatusResponse])
def get_data_sources(db: Session = Depends(get_db)) -> list[DataSourceStatusResponse]:
    """Postgres ingestion status per raw extract table I13 reads, from
    ``ingestion_run`` -- the real replacement for the old CSV-gateway
    LIVE/MOCK diagnostic, which no longer applies."""
    results = []
    for table in _I13_TABLES:
        run = db.execute(
            select(IngestionRun)
            .where(IngestionRun.target_table == table, IngestionRun.status == "succeeded")
            .order_by(IngestionRun.finished_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if run is not None:
            results.append(
                DataSourceStatusResponse(table=table, row_count=run.row_count, status="LOADED", loaded_at=run.finished_at)
            )
        else:
            results.append(DataSourceStatusResponse(table=table, row_count=0, status="NOT_LOADED", loaded_at=None))
    return results

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
from app.api.i13 import assistant as assistant_routes
from app.api.i13 import consumption_attribution as consumption_attribution_routes
from app.api.i13 import exceptions as exceptions_routes
from app.api.i13 import ledger as ledger_routes
from app.api.i13 import movement_metrics as movement_metrics_routes
from app.api.i13 import procurement_chain as procurement_chain_routes
from app.api.i13 import quantity_suggestion as quantity_suggestion_routes
from app.api.i13 import reclassification as reclassification_routes
from app.api.i13 import reservation_ledger as reservation_ledger_routes
from app.api.i13 import validation as validation_routes
from app.api.i13 import watch as watch_routes
from app.api.i13 import usage as usage_routes
from app.api.i13.deps import get_data_dir, snapshot_or_live
from app.core.db import get_db
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.snapshot import I13Snapshot, current_plans, snapshot_status, start_background_build
from app.initiatives.i13.summary import build_summary, summary_from_snapshot
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.models import IngestionRun
from app.schemas.i13 import DataSourceStatusResponse, I13SummaryResponse, SnapshotStatusResponse

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
# WS7's I13 paths: the FR-4 consumption-plan capture, and the FR-3 suggestion
# computed on demand.
#
# THIS MOUNT WAS MISSING. `assistant_routes` was imported at the top of this
# module and never included, so `POST /api/i13/consumption-plans` -- the capture
# point the FRS calls the single one (§3.1, D8) -- answered 404 at runtime while
# reading as built in the source. `tests/test_write_paths.py` had been failing
# on it in both directions. `test_every_imported_router_is_mounted` in
# tests/i13/test_i13_routes.py now makes a repeat impossible.
#
# It MUST be registered before quantity_suggestion_routes, for the same reason
# procurement_chain_routes precedes reservation_ledger_routes above: its
# "/quantity-suggestion/compute" is a literal that would otherwise be matched
# first by that router's "/quantity-suggestion/{suggestion_id}". Suggestion ids
# are `uuid4().hex`, so "compute" can never be a real one -- the two are
# distinguishable, and only the ordering makes them distinguished.
router.include_router(assistant_routes.router)
router.include_router(quantity_suggestion_routes.router)
# GRNI and usage patterns: served from the snapshot only (see usage.py).
router.include_router(usage_routes.router)

# The raw extract tables I13 actually reads -- see app/seed/manifest.py for
# the full delivery; this is the I13-relevant subset.
_I13_TABLES = ("raw_eban", "raw_ekpo", "raw_ekbe", "raw_mseg", "raw_mkpf", "raw_mard", "raw_resb", "raw_marc")


@router.get("/summary", response_model=I13SummaryResponse)
def get_summary(
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
    data_dir: Path = Depends(get_data_dir),
    snapshot: I13Snapshot | None = Depends(snapshot_or_live),
) -> I13SummaryResponse:
    if snapshot is not None:
        return I13SummaryResponse(**asdict(summary_from_snapshot(snapshot, current_plans(db, snapshot))))

    movement_repo = PostgresMovementRepository(db)
    procurement_repo = PostgresProcurementRepository(db)
    reservation_repo = PostgresReservationRepository(db)
    material_scope_index = fetch_material_scope_index(db)

    summary = build_summary(movement_repo, procurement_repo, reservation_repo, material_scope_index, config, data_dir)
    return I13SummaryResponse(**asdict(summary))


@router.get("/snapshot", response_model=SnapshotStatusResponse)
def get_snapshot_status() -> SnapshotStatusResponse:
    """What the I13 screens are being served from: when it was built, as of
    which date, and whether a rebuild is running."""
    return SnapshotStatusResponse(**snapshot_status())


@router.post("/snapshot/refresh", response_model=SnapshotStatusResponse, status_code=202)
def refresh_snapshot() -> SnapshotStatusResponse:
    """Rebuild the I13 snapshot in the background. The current one keeps
    serving until the new one is ready; poll ``GET /i13/snapshot``."""
    start_background_build("manual refresh")
    return SnapshotStatusResponse(**snapshot_status())


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

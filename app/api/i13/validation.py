"""GET /api/i13/validation -- local reconciliation against reference reports.

No real ZMM065 / 30-Day GR Report export exists in this repository yet, so
reference counts are accepted as optional query params; without them the
result is ``REFERENCE_UNAVAILABLE`` (see ``initiatives/i13/reconciliation.py``).

Reconciles against W6.1's procurement chain (``build_procurement_chain``,
PO-item-anchored), not W6.2's reservation-anchored ledger -- ZMM065 is a
plant-wide procurement/aging report at the PR/PO-item grain, the same grain
this reconciliation used before the CSV-to-Postgres migration.
"""

from pathlib import Path
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.i13.deps import get_data_dir, snapshot_or_live
from app.core.db import get_db
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.exceptions import build_exception_queue
from app.initiatives.i13.models import ExceptionType
from app.initiatives.i13.procurement_chain import build_procurement_chain
from app.initiatives.i13.reconciliation import reconcile
from app.initiatives.i13.snapshot import I13Snapshot, current_plans, exception_queue
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.schemas.i13 import ReconciliationSourceResult, ValidationResponse

router = APIRouter()


@router.get("/validation", response_model=ValidationResponse)
def get_validation(
    zmm065_reference_count: int | None = Query(None, description="Reference row count from the ZMM065 report"),
    gr_30_day_reference_count: int | None = Query(
        None, description="Reference count from the 30-Day GR Report"
    ),
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
    data_dir: Path = Depends(get_data_dir),
    snapshot: I13Snapshot | None = Depends(snapshot_or_live),
) -> ValidationResponse:
    if snapshot is not None:
        procurement_entries = snapshot.procurement_chain
        exceptions = exception_queue(snapshot, current_plans(db, snapshot))
    else:
        procurement_repo = PostgresProcurementRepository(db)
        movement_repo = PostgresMovementRepository(db)
        reservation_repo = PostgresReservationRepository(db)
        material_scope_index = fetch_material_scope_index(db)

        procurement_entries = build_procurement_chain(procurement_repo)
        exceptions = build_exception_queue(
            movement_repo, procurement_repo, reservation_repo, material_scope_index, config, data_dir
        )
    gr_not_issued_count = sum(1 for item in exceptions if item.type is ExceptionType.GR_NOT_ISSUED_30_DAY)

    results = [
        reconcile(
            "ZMM065",
            len(procurement_entries),
            zmm065_reference_count,
            tolerance_pct=config.reconciliation.tolerance_pct,
        ),
        reconcile(
            "30-Day GR Report",
            gr_not_issued_count,
            gr_30_day_reference_count,
            tolerance_pct=config.reconciliation.tolerance_pct,
        ),
    ]
    return ValidationResponse(
        tolerance_pct=Decimal(str(config.reconciliation.tolerance_pct)),
        results=[ReconciliationSourceResult.model_validate(result) for result in results],
    )

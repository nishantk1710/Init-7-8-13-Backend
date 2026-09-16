"""GET /api/i13/exceptions."""

from pathlib import Path

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.i13.deps import get_data_dir
from app.core.db import get_db
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.exceptions import build_exception_queue
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.schemas.i13 import ExceptionResponse

router = APIRouter()


@router.get("/exceptions", response_model=list[ExceptionResponse])
def list_exceptions(
    plant: str | None = Query(None),
    material: str | None = Query(None),
    exception_type: str | None = Query(None),
    exception_status: str | None = Query(None, alias="status"),
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
    data_dir: Path = Depends(get_data_dir),
) -> list[ExceptionResponse]:
    movement_repo = PostgresMovementRepository(db)
    procurement_repo = PostgresProcurementRepository(db)
    reservation_repo = PostgresReservationRepository(db)
    # material/plant now push all the way down into build_exception_queue's
    # underlying builds (real SQL filters), not just a client-side trim of
    # an unfiltered, tenant-wide result -- see that function's docstring for
    # why this matters at real data scale. The scope index shares the same
    # filter, since build_exception_queue's own fetches now do too.
    material_scope_index = fetch_material_scope_index(db, material=material, plant=plant)

    items = build_exception_queue(
        movement_repo, procurement_repo, reservation_repo, material_scope_index, config, data_dir,
        material=material, plant=plant,
    )
    if exception_type:
        items = [item for item in items if item.type.value == exception_type.upper()]
    if exception_status:
        items = [item for item in items if item.status.value == exception_status.upper()]
    return [ExceptionResponse.model_validate(item) for item in items]

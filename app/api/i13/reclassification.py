"""GET /api/i13/reclassification."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.reclassification import build_reclassification_candidates
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.schemas.i13 import ReclassificationCandidateResponse

router = APIRouter()


@router.get("/reclassification", response_model=list[ReclassificationCandidateResponse])
def list_reclassification_candidates(
    plant: str | None = Query(None),
    material: str | None = Query(None),
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
) -> list[ReclassificationCandidateResponse]:
    movement_repo = PostgresMovementRepository(db)
    material_scope_index = fetch_material_scope_index(db, material=material, plant=plant)

    candidates = build_reclassification_candidates(
        movement_repo, material_scope_index, config, material=material, plant=plant
    )
    return [ReclassificationCandidateResponse.model_validate(candidate) for candidate in candidates]

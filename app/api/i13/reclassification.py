"""GET /api/i13/reclassification."""

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.i13.deps import page, snapshot_or_live
from app.core.db import get_db
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.reclassification import build_reclassification_candidates
from app.initiatives.i13.snapshot import I13Snapshot
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.schemas.i13 import ReclassificationCandidateResponse

router = APIRouter()


@router.get("/reclassification", response_model=list[ReclassificationCandidateResponse])
def list_reclassification_candidates(
    response: Response,
    plant: str | None = Query(None),
    material: str | None = Query(None),
    candidates_only: bool = Query(False, description="Only rows with candidate_flag set."),
    limit: int | None = Query(None, ge=1, le=50000, description="Unbounded when omitted, as before."),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
    snapshot: I13Snapshot | None = Depends(snapshot_or_live),
) -> list[ReclassificationCandidateResponse]:
    if snapshot is not None:
        candidates = [
            c
            for c in snapshot.reclassification
            if (not plant or c.plant == plant) and (not material or c.material == material)
        ]
    else:
        movement_repo = PostgresMovementRepository(db)
        material_scope_index = fetch_material_scope_index(db, material=material, plant=plant)
        candidates = build_reclassification_candidates(
            movement_repo, material_scope_index, config, material=material, plant=plant
        )
    if candidates_only:
        candidates = [c for c in candidates if c.candidate_flag]
    return [
        ReclassificationCandidateResponse.model_validate(candidate)
        for candidate in page(candidates, response, limit=limit, offset=offset)
    ]

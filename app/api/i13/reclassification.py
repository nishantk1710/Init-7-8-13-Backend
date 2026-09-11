"""GET /api/i13/reclassification."""

from fastapi import APIRouter, Depends, Query

from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.reclassification import build_reclassification_candidates
from app.integrations.sap.gateway import SapGateway, get_sap_gateway
from app.schemas.i13 import ReclassificationCandidateResponse

router = APIRouter()


@router.get("/reclassification", response_model=list[ReclassificationCandidateResponse])
def list_reclassification_candidates(
    plant: str | None = Query(None),
    material: str | None = Query(None),
    gateway: SapGateway = Depends(get_sap_gateway),
    config: I13Config = Depends(get_i13_config),
) -> list[ReclassificationCandidateResponse]:
    candidates = build_reclassification_candidates(gateway, config)
    if plant:
        candidates = [candidate for candidate in candidates if candidate.plant == plant]
    if material:
        candidates = [candidate for candidate in candidates if candidate.material == material]
    return [ReclassificationCandidateResponse.model_validate(candidate) for candidate in candidates]

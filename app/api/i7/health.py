"""I07 liveness. Deliberately dependency-free -- see ``app.api.health`` for
why: it must work with no database, and it must never claim a dependency
(SAP, in particular) is healthy without having actually checked it."""

from datetime import datetime, timezone

from fastapi import APIRouter

from app.schemas.i7.health import I07HealthResponse

router = APIRouter(tags=["i7"])

API_VERSION = "v1"


@router.get(
    "/health",
    response_model=I07HealthResponse,
    summary="I07 liveness check",
    description="Confirms the I07 API is up. Touches no database, storage or "
    "SAP dependency -- see /api/v1/i7/runs for evidence that the pipeline "
    "itself has actually executed.",
)
def get_i07_health() -> I07HealthResponse:
    return I07HealthResponse(
        initiative="I07",
        status="healthy",
        api_version=API_VERSION,
        timestamp=datetime.now(timezone.utc),
    )

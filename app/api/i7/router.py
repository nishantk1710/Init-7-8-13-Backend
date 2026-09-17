"""The I07 API router: mounted once, at ``/api/v1/i7`` (see
``app.api.router``).

The exception-to-response mapping for ``ApiError`` (the consistent
``{"error": {"code", "message", "details"}}`` envelope) is registered on the
application itself in ``app.main`` -- FastAPI exception handlers are
app-level, not per-router -- so it applies uniformly to every I07 route
without any route formatting its own error body.
"""

from fastapi import APIRouter

from app.api.i7 import adoption, approval_history, approvals, health, recommendations, runs

router = APIRouter(prefix="/v1/i7")

router.include_router(health.router)
router.include_router(recommendations.router)
router.include_router(approvals.router)
router.include_router(approval_history.router)
router.include_router(adoption.router)
router.include_router(runs.router)

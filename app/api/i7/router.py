"""The I07 API router: mounted once, at ``/api/v1/i7`` (see
``app.api.router``).

The exception-to-response mapping for ``ApiError`` (the consistent
``{"error": {"code", "message", "details"}}`` envelope) is registered on the
application itself in ``app.main`` -- FastAPI exception handlers are
app-level, not per-router -- so it applies uniformly to every I07 route
without any route formatting its own error body.
"""

from fastapi import APIRouter

from app.api.i7 import adoption, approval_history, approvals, health, recommendations, reports, runs

router = APIRouter(prefix="/v1/i7")

router.include_router(health.router)
# adoption.router before recommendations.router: GET /recommendations/adoption
# (adoption's own list route) would otherwise be swallowed by
# recommendations.py's GET /recommendations/{recommendation_id}, which binds
# "adoption" as a recommendation_id -- FastAPI matches included routers'
# routes in registration order.
router.include_router(adoption.router)
router.include_router(recommendations.router)
router.include_router(approvals.router)
router.include_router(approval_history.router)
router.include_router(runs.router)
# reports.py's paths are all under /reports/quarterly/... -- no collision
# with runs.py's /runs/... or recommendations.py's /recommendations/...
# prefixes, so registration order relative to those routers does not matter.
# Order WITHIN reports.py itself does, and is verified there: the list route
# is declared before the /{quarter} routes, and /generate and
# /{quarter}/status|export are declared before the catch-all /{quarter}, so
# "quarterly" (with nothing after it), "generate", "status" and "export" are
# never swallowed as a quarter value.
router.include_router(reports.router)

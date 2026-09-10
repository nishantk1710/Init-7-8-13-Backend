"""Central API router.

Every route in the application is mounted here, and this router is mounted once
in ``app.main`` under the configured API prefix. Initiative routers are
registered here as they are built -- see the commented extension points below.
"""

from fastapi import APIRouter

from app.api import health
from app.api.events import pr

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(pr.router)

# --- Extension points: one Spares AI backend, three business modules. ---
# from app.api.i7.router import router as i7_router
# from app.api.i8.router import router as i8_router
# from app.api.i13.router import router as i13_router
#
# api_router.include_router(i7_router)    # -> /api/i7/*
# api_router.include_router(i8_router)    # -> /api/i8/*
# api_router.include_router(i13_router)   # -> /api/i13/*

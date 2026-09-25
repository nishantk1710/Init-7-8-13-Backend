"""Central API router.

Every route in the application is mounted here, and this router is mounted once
in ``app.main`` under the configured API prefix. Initiative routers are
registered here as they are built -- see the commented extension points below.
"""

from fastapi import APIRouter

from app.api import health, ready
from app.api.assistant.router import justifications_router
from app.api.assistant.router import router as assistant_router
from app.api.events import csv_upload, pr
from app.api.i7.router import router as i7_router
from app.api.i13.routes import router as i13_router

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(ready.router)
api_router.include_router(pr.router)
api_router.include_router(i7_router)  # -> /api/v1/i7/*
api_router.include_router(i13_router)  # -> /api/i13/*

# W7: the shared reservation-time assistant. Mounted OUTSIDE both initiative
# prefixes on purpose -- the BAdI pop-up knows a material and a plant, and
# cannot know whether that material is 80-series or OAR, so it cannot pick a
# prefix. See app/assistant/router.py.
api_router.include_router(assistant_router)       # -> /api/assistant/*
api_router.include_router(justifications_router)  # -> /api/justifications
api_router.include_router(csv_upload.router)

# --- Extension points: one Spares AI backend, three business modules. ---
from app.api.i8.router import router as i8_router

api_router.include_router(i8_router)      # -> /api/i8/*

#

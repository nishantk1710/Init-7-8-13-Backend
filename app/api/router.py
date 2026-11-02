"""Central API router.

Every route in the application is mounted here, and this router is mounted once
in ``app.main`` under the configured API prefix. Initiative routers are
registered here as they are built -- see the commented extension points below.

Sign-in (``app/core/security.py``) is wired here, once, rather than in every
route file:

- every business router carries ``require_signed_in`` -- the read rule: a
  signed-in person holding at least one ``Spares.*`` role. Write routes add
  their own ``require_roles(...)`` on top;
- the health checks and ``/api/me`` stay open to anyone (``/api/me`` checks the
  token itself, so a user without a role still learns that);
- SAP's ``/api/events/*`` carry ``require_sap_events`` instead, switched by
  ``SAP_EVENTS_AUTH``.

With ``AUTH_ENABLED=false`` (the default) all of these let every request
through, exactly as before.
"""

from fastapi import APIRouter, Depends

from app.api import health, me, ready
from app.api.assistant.router import justifications_router
from app.api.assistant.router import router as assistant_router
from app.api.assistant.requests_router import router as assistant_requests_router
from app.api.criticality.router import router as criticality_router
from app.api.events import csv_upload, pr
from app.api.i7.router import router as i7_router
from app.api.i13.routes import router as i13_router
from app.core.security import require_sap_events, require_signed_in

#: The read rule, applied to every business router below.
SIGNED_IN = [Depends(require_signed_in)]

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(ready.router)
api_router.include_router(me.router)  # -> /api/me
api_router.include_router(pr.router, dependencies=[Depends(require_sap_events)])
api_router.include_router(i7_router, dependencies=SIGNED_IN)  # -> /api/v1/i7/*
api_router.include_router(i13_router, dependencies=SIGNED_IN)  # -> /api/i13/*

# W7: the shared reservation-time assistant. Mounted OUTSIDE both initiative
# prefixes on purpose -- the BAdI pop-up knows a material and a plant, and
# cannot know whether that material is 80-series or OAR, so it cannot pick a
# prefix. See app/assistant/router.py.
api_router.include_router(assistant_router, dependencies=SIGNED_IN)       # -> /api/assistant/*
api_router.include_router(assistant_requests_router, dependencies=SIGNED_IN)  # -> /api/assistant/requests/* (flag-gated)
api_router.include_router(justifications_router, dependencies=SIGNED_IN)  # -> /api/justifications
api_router.include_router(csv_upload.router, dependencies=[Depends(require_sap_events)])

# The monthly criticality reports feed I07, I08 and I13 alike, so like the
# assistant they sit outside every initiative prefix.
api_router.include_router(criticality_router, dependencies=SIGNED_IN)  # -> /api/criticality/*

# --- Extension points: one Spares AI backend, three business modules. ---
from app.api.i8.router import router as i8_router

api_router.include_router(i8_router, dependencies=SIGNED_IN)      # -> /api/i8/*

#

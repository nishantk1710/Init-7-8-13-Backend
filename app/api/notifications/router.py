"""Notification endpoints, mounted under ``/api/notifications``.

* the signed-in user's own inbox (``inbox.py``)
* administrator endpoints for recipients and SLA timings (``admin.py``)
"""

from fastapi import APIRouter

from app.api.notifications import admin, inbox

router = APIRouter()
# Admin first: its two-segment paths (/admin/recipients) must not be read as /{notification_id}.
router.include_router(admin.router, prefix="/notifications")
router.include_router(inbox.router)

"""In-app notice that SAP sent a purchase requisition (the PR event listener).

Who hears what
--------------
* **Everyone who has signed in** (``app_user``) is told that a requisition arrived --
  and nothing else. The title and text name no PR number, creator or message, and the
  row's ``entity_id`` -- which the inbox API returns and the inbox page shows as
  "Ref ..." -- is left empty. The event is told apart by an opaque ``trigger_id``
  derived from it, which only feeds the idempotency key.
* **Administrators** -- the active ``Spares.Admin`` rows in ``notification_recipients``,
  at any plant (the event carries no plant) -- get one notice with the details instead
  of the plain one: PR number, created on/at, the SAP user who created it, SAP's message.
  With no administrator configured, that gap is recorded as a failed row, like every
  other unresolved recipient.

In-app only, on purpose. An email to every user for every requisition would be noise;
the inbox is where this belongs, and the email switch is left to the workflow notices.

The event's ``CREATED_BY`` is a SAP user name, not an Entra identity, so it can only
be shown (to administrators), never notified.

A repeat of the same event (same BANFN, date and time) adds nothing: the idempotency
key is derived from them. An event without a BANFN cannot be matched to a repeat, so
each one is its own notice.

``notify_in_background`` runs after SAP has its 202 (a FastAPI background task), so a
slow or unreachable database never delays or fails the event. It never raises.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import Roles
from app.integrations.notifications import templates
from app.integrations.notifications.models import Channel, NotificationEvent, Recipient
from app.integrations.notifications.resolver import is_entra_object_id
from app.integrations.notifications.service import enqueue
from app.models.app_user import AppUser
from app.models.notification import NotificationRecipient

logger = logging.getLogger(__name__)

MODULE = "SAP_EVENTS"
ENTITY_TYPE = "PURCHASE_REQUISITION"
_IN_APP = (Channel.IN_APP,)


def _fields(payload: Mapping[str, Any]) -> dict[str, str]:
    """The event's fields by upper-case name, trimmed. SAP's casing is not guaranteed."""
    return {str(k).upper(): str(v).strip() for k, v in payload.items() if v not in (None, "")}


def _sap_date(value: str) -> str:
    """``20260916`` -> ``2026-09-16``; anything else as SAP sent it."""
    return f"{value[:4]}-{value[4:6]}-{value[6:]}" if len(value) == 8 and value.isdigit() else value


def _sap_time(value: str) -> str:
    """``131403`` -> ``13:14:03``; anything else as SAP sent it."""
    return f"{value[:2]}:{value[2:4]}:{value[4:]}" if len(value) == 6 and value.isdigit() else value


def event_id(payload: Mapping[str, Any], received_utc: str) -> str:
    """One opaque id per requisition event, used only as the notices' ``trigger_id`` (so
    in the idempotency key, which the inbox API never returns). Hashed all the same, so
    no column of a non-administrator's row holds the BANFN."""
    f = _fields(payload)
    basis = "|".join((f.get("BANFN", ""), f.get("CREATED_ON", ""), f.get("CREATED_AT", ""))) if f.get("BANFN") else (
        f"no-banfn|{received_utc}"
    )
    return "pr-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def administrators(db: Session) -> list[Recipient]:
    """The active Spares.Admin recipients with an Entra object id, at any plant, once each."""
    rows = db.scalars(
        select(NotificationRecipient)
        .where(NotificationRecipient.role == Roles.ADMIN, NotificationRecipient.active)
        .order_by(NotificationRecipient.id)
    )
    found: dict[str, Recipient] = {}
    for row in rows:
        oid = (row.user_oid or "").strip()
        if is_entra_object_id(oid):
            found.setdefault(oid.lower(), Recipient(user_oid=oid, role=Roles.ADMIN))
    return list(found.values())


def signed_in_users(db: Session) -> list[str]:
    """Every Entra object id that has signed in, once each. ``app_user`` keeps a row per
    name or e-mail change, and may hold placeholders recorded before sign-in: skipped."""
    found: dict[str, str] = {}
    for oid in db.scalars(select(AppUser.oid).distinct()):
        value = (oid or "").strip()
        if is_entra_object_id(value):
            found.setdefault(value.lower(), value)
    return sorted(found.values())


def notify_pr_received(db: Session, payload: Mapping[str, Any], *, received_utc: str) -> dict[str, int]:
    """Write the in-app notices for one PR event. Does not commit. Returns the counts."""
    f = _fields(payload)
    opaque = event_id(payload, received_utc)
    admins = administrators(db)
    admin_ids = {r.user_oid.lower() for r in admins if r.user_oid}
    everyone = [Recipient(user_oid=oid) for oid in signed_in_users(db) if oid.lower() not in admin_ids]

    written = 0
    if everyone:
        plain = NotificationEvent(
            module=MODULE,
            event_type=templates.PR_RECEIVED,
            entity_type=ENTITY_TYPE,
            entity_id="",  # nothing to show: the inbox page hides an empty reference
            trigger_id=opaque,
        )
        written += len(enqueue(db, plain, everyone, channels=_IN_APP, email_enabled=False))

    details = NotificationEvent(
        module=MODULE,
        event_type=templates.PR_RECEIVED_DETAILS,
        entity_type=ENTITY_TYPE,
        entity_id=f.get("BANFN", ""),
        trigger_id=opaque,
        context={
            "banfn": f.get("BANFN", ""),
            "created_on": _sap_date(f.get("CREATED_ON", "")),
            "created_at": _sap_time(f.get("CREATED_AT", "")),
            "created_by": f.get("CREATED_BY", ""),
            "message": f.get("MESSAGE", ""),
        },
    )
    to_admins = admins or [Recipient(role=Roles.ADMIN, error=f"no recipient configured for role {Roles.ADMIN}")]
    written += len(enqueue(db, details, to_admins, channels=_IN_APP, email_enabled=False))
    return {"users": len(everyone), "admins": len(admins), "written": written}


def notify_in_background(payload: Mapping[str, Any], received_utc: str) -> None:
    """The background task the PR endpoint schedules. Never raises."""
    settings = get_settings()
    if not settings.pr_event_notifications_enabled or not settings.database_url:
        return
    banfn = _fields(payload).get("BANFN", "(no BANFN)")
    try:
        from app.core.db import get_sessionmaker

        with get_sessionmaker()() as db:
            counts = notify_pr_received(db, payload, received_utc=received_utc)
            db.commit()
        logger.info(
            "PR %s: in-app notice to %d signed-in user(s), details to %d administrator(s), %d new row(s)",
            banfn, counts["users"], counts["admins"], counts["written"],
        )
    except Exception:  # noqa: BLE001 -- SAP already has its 202; a notice must never turn into an error
        logger.exception("PR %s: in-app notices could not be written", banfn)

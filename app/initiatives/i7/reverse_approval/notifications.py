"""Who is told when a Min-Max -> OAR conversion request moves.

The same people are told, at the same moments, as for an OAR -> Min-Max approval (``app.initiatives.i7.notifications``):
the role whose turn it now is hears that a request is waiting for them, and the person who raised it hears how it
ended. The approver comes from the request's own chain position, so a change to the route changes who is told with no
edit here. The message links to the Stock item to OAR tab, filtered to the material.

Called by the request routes right after the step succeeded and just before the route commits, so the notifications
are saved in the same transaction as the step. A notification that cannot be queued is logged and never blocks the
approval itself.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.initiatives.i7.notifications import MODULE, role_holders
from app.initiatives.i7.recommendations.types import ApprovalRole
from app.initiatives.i7.reverse_approval import service
from app.integrations.notifications import templates
from app.integrations.notifications.models import NotificationEvent, Recipient
from app.integrations.notifications.resolver import resolve_user
from app.integrations.notifications.service import enqueue
from app.models.i7_reverse import ReverseApprovalLedger, ReverseConversionRequest

logger = logging.getLogger(__name__)

ENTITY_TYPE = "REVERSE_REQUEST"
"""The notification's ``entity_type``: a Min-Max -> OAR request (not a RECOMMENDATION), so ids can never collide."""

_ENTRY_ACTIONS = ("SUBMIT", "RESUBMIT")


def request_path(material: str, plant: str, *, decide: bool = False) -> str:
    """Where the message leads.

    ``decide``: the recipient is asked to act, so the link opens the decision page (where the pending role approves,
    sends back, holds or rejects). Otherwise it opens the Stock item to OAR tab filtered to the material, where the
    request's state can be read -- the same split as the OAR route's ``?review=1`` link."""
    query = f"material={quote(material, safe='')}&plant={quote(plant, safe='')}"
    if decide:
        return f"/inventory-planning/convert-to-oar?{query}"
    return f"/inventory-planning/recommendations?type=min_max_to_oar&{query}"


def _latest_entry(db: Session, request_id: int) -> ReverseApprovalLedger | None:
    entries = service.history(db, request_id)
    return entries[-1] if entries else None


def _submitter(db: Session, request: ReverseConversionRequest) -> list[Recipient]:
    return resolve_user(db, request.submitted_by, fallback_email=get_settings().notification_fallback_email)


def _holders(db: Session, role: ApprovalRole | None, plant: str) -> list[Recipient]:
    return role_holders(db, role, plant) if role is not None else []


def notify_request_transition(db: Session, request: ReverseConversionRequest) -> None:
    """Queue the notifications for the step that was just recorded. Does not commit.

    The step's own writes are flushed first, outside the guard: a failure there is the approval's and must surface.
    Everything after runs in a savepoint, so a notification that goes wrong rolls back alone and the approval commits."""
    db.flush()
    try:
        with db.begin_nested():
            _queue(db, request)
    except Exception as exc:  # noqa: BLE001 -- the approval step itself already succeeded
        logger.warning(
            "I07 notification not queued for Min-Max to OAR request %s (%s); the approval is unaffected",
            request.id, type(exc).__name__,
        )
        logger.debug("I07 reverse request notification failure detail", exc_info=True)


def _queue(db: Session, request: ReverseConversionRequest) -> None:
    entry = _latest_entry(db, request.id)
    if entry is None:
        return

    material, plant = request.sap_material_number, request.sap_plant_code
    waiting = service.pending_role(request)
    action = entry.action
    action_line = ""
    if action in _ENTRY_ACTIONS or action == "APPROVE":
        if waiting is not None:
            event_type, to_whom = templates.APPROVAL_REQUIRED, _holders(db, waiting, plant)
            title = "Approval Required – Convert to OAR"
            lead = f"A request to convert a Min-Max material to OAR for Plant {plant} is waiting for your approval."
            action_line = "Action required: review the request and approve, send back, hold or reject it."
        else:
            event_type, to_whom = templates.APPROVAL_COMPLETED, _submitter(db, request)
            title = f"Convert to OAR Approved – Material {material}"
            lead = f"A request to convert a Min-Max material to OAR for Plant {plant} has been fully approved."
            action_line = "Next step: the change in SAP is made by the team. Nothing is written to SAP from this page."
    elif action == "SEND_BACK":
        event_type, to_whom = templates.APPROVAL_SENT_BACK, _holders(db, waiting, plant) + _submitter(db, request)
        title = f"Convert to OAR Sent Back – Material {material}"
        lead = f"A request to convert a Min-Max material to OAR for Plant {plant} was sent back."
    elif action == "REJECT":
        event_type, to_whom = templates.APPROVAL_REJECTED, _submitter(db, request)
        title = f"Convert to OAR Rejected – Material {material}"
        lead = f"A request to convert a Min-Max material to OAR for Plant {plant} was rejected."
    elif action == "HOLD":
        event_type, to_whom = templates.APPROVAL_ON_HOLD, _submitter(db, request)
        title = f"Convert to OAR On Hold – Material {material}"
        lead = f"A request to convert a Min-Max material to OAR for Plant {plant} was put on hold by the approver."
    elif action == "RELEASE_HOLD":
        event_type, to_whom = templates.APPROVAL_HOLD_RELEASED, _submitter(db, request) + _holders(db, waiting, plant)
        title = f"Convert to OAR Hold Released – Material {material}"
        lead = f"The hold on a request to convert a Min-Max material to OAR for Plant {plant} was released and it is moving again."
    else:
        return

    # The person who just acted needs no message about their own action -- except an approver asked to decide.
    if event_type != templates.APPROVAL_REQUIRED:
        to_whom = [r for r in to_whom if r.error or r.user_oid != entry.actor_id]
    if not to_whom:
        return

    lines = [lead, f"Material: {material}"]
    if entry.comment:
        lines.append(f"Comment: {entry.comment}")
    if action_line:
        lines.append(action_line)
    enqueue(
        db,
        NotificationEvent(
            module=MODULE,
            event_type=event_type,
            entity_type=ENTITY_TYPE,
            entity_id=str(request.id),
            trigger_id=str(entry.id),
            action_path=request_path(
                material, plant, decide=event_type in (templates.APPROVAL_REQUIRED, templates.APPROVAL_HOLD_RELEASED)
            ),
            plant=plant,
            title=title,
            description="\n".join(lines),
        ),
        to_whom,
    )

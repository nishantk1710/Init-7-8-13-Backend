"""Who is told when an approval moves.

Called by the approval routes right after the workflow step succeeded and just
before the route commits, so the notifications are saved in the same
transaction as the step. A step that fails (HTTP 409) raises before this runs,
so nothing is sent for it.

Nothing here knows a route. The next approver comes from the recommendation's
own route and chain position -- the same ``routing.route_for`` and
``WorkflowState.pending_role`` the workflow itself uses -- so a policy change to
a route changes who is notified with no edit here.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import Roles
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations import routing, workflow
from app.initiatives.i7.recommendations.types import ApprovalRole, LifecycleStatus
from app.integrations.notifications import templates
from app.integrations.notifications.models import NotificationEvent, Recipient
from app.integrations.notifications.resolver import resolve_role, resolve_user
from app.integrations.notifications.service import enqueue
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation

logger = logging.getLogger(__name__)

MODULE = "I07"

#: The Entra app role behind each approval step. The Inventory Controller step is
#: ``InventoryControllerApprover`` (``security.APPROVAL_STAGE_ROLES`` lists it as
#: the stage role); ``Spares.InventoryController`` is a different, older role.
#: The team should confirm that pairing.
APPROVAL_ROLE_TO_ENTRA: dict[ApprovalRole, str] = {
    ApprovalRole.END_USER: Roles.END_USER,
    ApprovalRole.ENGINEERING_MANAGER: Roles.ENGINEERING_MANAGER,
    ApprovalRole.COMMERCIAL_MANAGER: Roles.COMMERCIAL_MANAGER,
    ApprovalRole.WAREHOUSE_SUPERVISOR: Roles.WAREHOUSE_SUPERVISOR,
    ApprovalRole.INVENTORY_CONTROLLER: Roles.INVENTORY_CONTROLLER_APPROVER,
    ApprovalRole.COMMERCIAL_HEAD: Roles.COMMERCIAL_HEAD,
    ApprovalRole.ENGINEERING_HEAD: Roles.ENGINEERING_HEAD,
    ApprovalRole.PLANT_HEAD: Roles.PLANT_HEAD,
}

#: Actions that start or resume an approver's turn.
_ENTRY_ACTIONS = ("SUBMIT", "RESUBMIT")


def review_path(recommendation_id: str, *, decide: bool) -> str:
    """The recommendation's page in the review workspace; ``?review=1`` opens it ready to decide."""
    path = f"/inventory-planning/recommendations/{quote(recommendation_id, safe='')}"
    return f"{path}?review=1" if decide else path


def pending_role(row: Recommendation, policy: PolicyDocument) -> ApprovalRole | None:
    """The role whose turn it is now, or ``None`` when nobody's is (finished, or never submitted)."""
    state = workflow.WorkflowState(
        status=LifecycleStatus(row.status),
        chain_index=row.chain_index,
        adjustment_count=row.adjustment_count,
        route=routing.route_for(row.is_oar, row.criticality, policy),
    )
    return state.pending_role


def role_holders(db: Session, role: ApprovalRole, plant: str | None) -> list[Recipient]:
    return resolve_role(
        db, APPROVAL_ROLE_TO_ENTRA[role], plant, fallback_email=get_settings().notification_fallback_email
    )


def _latest_ledger_entry(db: Session, recommendation_id: str) -> ApprovalLedgerEntry | None:
    return db.scalars(
        select(ApprovalLedgerEntry)
        .where(ApprovalLedgerEntry.recommendation_id == recommendation_id)
        .order_by(ApprovalLedgerEntry.id.desc())
        .limit(1)
    ).first()


def _submitter_and_earlier_approvers(db: Session, recommendation_id: str) -> tuple[str | None, list[str]]:
    """Who last submitted it, and who approved a step since."""
    entries = list(
        db.scalars(
            select(ApprovalLedgerEntry)
            .where(ApprovalLedgerEntry.recommendation_id == recommendation_id)
            .order_by(ApprovalLedgerEntry.id)
        )
    )
    submitter: str | None = None
    approvers: list[str] = []
    for entry in entries:
        if entry.action in _ENTRY_ACTIONS:
            submitter, approvers = entry.actor_id, []
        elif entry.action == "APPROVE" and entry.actor_id not in approvers:
            approvers.append(entry.actor_id)
    return submitter, approvers


def _without(recipients: list[Recipient], actor_id: str) -> list[Recipient]:
    """Drop the person who just acted -- they need no message about their own action."""
    return [r for r in recipients if r.error or r.user_oid != actor_id]


def notify_transition(db: Session, row: Recommendation, policy: PolicyDocument) -> None:
    """Queue the notifications for the workflow step that was just recorded.

    Reads the ledger entry the step wrote, so it works for submit, reject and
    every approval action alike. Does not commit.

    The step's own writes are flushed first, *outside* the guard below: a failure
    there is an approval failure and must surface. Everything after that runs in
    a savepoint, so if anything about the notification goes wrong -- including the
    database refusing a row -- only the notification is rolled back, the caller's
    session stays usable and the approval still commits. The failure is logged with
    context but without the values involved.
    """
    db.flush()  # the ledger entry gets its id; an error here is the approval's, not ours
    try:
        with db.begin_nested():
            _queue(db, row, policy)
    except Exception as exc:  # noqa: BLE001 -- the approval step itself already succeeded
        logger.warning(
            "I07 notification not queued for recommendation %s (%s); the approval is unaffected",
            row.recommendation_id, type(exc).__name__,
        )
        logger.debug("I07 notification failure detail", exc_info=True)


def _queue(db: Session, row: Recommendation, policy: PolicyDocument) -> None:
    entry = _latest_ledger_entry(db, row.recommendation_id)
    if entry is None:
        return

    waiting = pending_role(row, policy)
    submitter, earlier_approvers = _submitter_and_earlier_approvers(db, row.recommendation_id)
    fallback = get_settings().notification_fallback_email
    submitter_recipients = resolve_user(db, submitter, fallback_email=fallback) if submitter else []

    action = entry.action
    to_decide = True  # whether the recipients are asked to act (so the link opens ready to decide)
    if action in _ENTRY_ACTIONS or action == "APPROVE":
        if waiting is not None:
            event_type, recipients = templates.APPROVAL_REQUIRED, role_holders(db, waiting, row.sap_plant_code)
        else:
            event_type, to_decide = templates.APPROVAL_COMPLETED, False
            # The last approver in this recommendation's route just approved: the Inventory Controller is told
            # what to change in SAP, and by when (adoption_notices.py also reminds them while the window is open).
            from app.initiatives.i7 import adoption_notices

            adoption_notices.queue_handover_notice(db, row, entry, policy)
            recipients = list(submitter_recipients)
            executor = get_settings().notification_sap_execution_role.strip()
            if executor:
                recipients += resolve_role(db, executor, row.sap_plant_code, fallback_email=get_settings().notification_fallback_email)
    elif action == "SEND_BACK":
        event_type, to_decide = templates.APPROVAL_SENT_BACK, False
        recipients = (role_holders(db, waiting, row.sap_plant_code) if waiting else []) + submitter_recipients
    elif action == "REJECT":
        event_type, to_decide = templates.APPROVAL_REJECTED, False
        recipients = submitter_recipients + [
            r for approver in earlier_approvers for r in resolve_user(db, approver, fallback_email=fallback)
        ]
    elif action == "HOLD":
        event_type, to_decide = templates.APPROVAL_ON_HOLD, False
        recipients = submitter_recipients
    elif action == "RELEASE_HOLD":
        event_type = templates.APPROVAL_HOLD_RELEASED
        recipients = submitter_recipients + (role_holders(db, waiting, row.sap_plant_code) if waiting else [])
    elif action == "ADJUST":
        # The recommendation has moved on to the next role, who is asked to review the changed values.
        event_type = templates.APPROVAL_ADJUSTED
        recipients = role_holders(db, waiting, row.sap_plant_code) if waiting else []
        # Whoever already saw the earlier values -- the submitter and the approvers so far -- is told, for
        # information only, that they changed.
        informed = list(submitter_recipients) + [
            r for approver in earlier_approvers for r in resolve_user(db, approver, fallback_email=fallback)
        ]
        informed = _without(informed, entry.actor_id)
        informed = [r for r in informed if r.error or r.user_oid not in {x.user_oid for x in recipients if not x.error}]
        if informed:
            enqueue(
                db,
                NotificationEvent(
                    module=MODULE,
                    event_type=templates.APPROVAL_ADJUSTED_FYI,
                    entity_type="RECOMMENDATION",
                    entity_id=row.recommendation_id,
                    trigger_id=str(entry.id),
                    action_path=review_path(row.recommendation_id, decide=False),
                    plant=row.sap_plant_code,
                    context={
                        "material": row.sap_material_number,
                        "comment": entry.comment or "",
                        "adjusted_by": entry.actor_role,
                        "pending_role": waiting.value if waiting else "",
                    },
                ),
                informed,
            )
    else:
        return

    if event_type != templates.APPROVAL_REQUIRED:
        recipients = _without(recipients, entry.actor_id)
    if not recipients:
        return

    enqueue(
        db,
        NotificationEvent(
            module=MODULE,
            event_type=event_type,
            entity_type="RECOMMENDATION",
            entity_id=row.recommendation_id,
            trigger_id=str(entry.id),
            action_path=review_path(row.recommendation_id, decide=to_decide),
            plant=row.sap_plant_code,
            context={
                "material": row.sap_material_number,
                "comment": entry.comment or "",
                "pending_role": waiting.value if waiting else "",
                "adjusted_by": entry.actor_role,
            },
        ),
        recipients,
    )

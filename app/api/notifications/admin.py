"""Administrator endpoints for who is notified and how soon.

Mounted under ``/api/notifications/admin`` and limited to ``Spares.Admin``.
They are the only way to see the recipient and SLA tables -- ordinary users
never get the full list. Every change writes an audit row in the same
transaction.

Like every write in this application they are POST only (no PUT or DELETE; see
``tests/test_write_paths.py``). A row is never deleted: retire it with
``active=false``, which keeps the history and the audit trail intact.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_current_actor
from app.core.db import get_db
from app.core.security import USER_ROLES, Roles, require_roles
from app.integrations.notifications import config_store
from app.integrations.notifications.resolver import is_entra_object_id
from app.models.notification import NotificationRecipient, NotificationSla

router = APIRouter(
    prefix="/admin",
    tags=["notifications-admin"],
    dependencies=[Depends(require_roles(Roles.ADMIN))],
)

APPROVAL_ROUTES = ("OAR", "ROP_MAX", "ALL")


def _known_role(value: str | None) -> str | None:
    if value is not None and value not in USER_ROLES:
        raise ValueError(f"unknown role {value!r}; use an Entra app role such as Spares.EngineeringHead")
    return value


# --- Recipients --------------------------------------------------------------


class RecipientIn(BaseModel):
    role: str
    plant: str | None = Field(default=None, max_length=8, description="Empty means every plant.")
    user_oid: str | None = Field(default=None, max_length=36)
    email: str | None = Field(default=None, max_length=320, description="Only for someone who has not signed in yet.")
    active: bool = True

    _check_role = field_validator("role")(_known_role)

    @field_validator("user_oid")
    @classmethod
    def _check_user_oid(cls, value: str | None) -> str | None:
        if value is not None and not is_entra_object_id(value):
            raise ValueError("user_oid must be an Entra object id (a GUID such as 11111111-2222-3333-4444-555555555555)")
        return value


class RecipientOut(RecipientIn):
    id: int
    updated_at: datetime | None = None
    updated_by: str | None = None

    model_config = {"from_attributes": True}


def _recipient_or_404(db: Session, recipient_id: int) -> NotificationRecipient:
    row = db.get(NotificationRecipient, recipient_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recipient not found")
    return row


def _needs_a_person(body: RecipientIn) -> None:
    if not body.user_oid and not body.email:
        raise HTTPException(422, "Give a user_oid, or an email for someone who has not signed in.")


@router.get("/recipients", response_model=list[RecipientOut], summary="List recipient assignments")
def list_recipients(db: Session = Depends(get_db)) -> list[NotificationRecipient]:
    return config_store.list_recipients(db)


@router.post("/recipients", response_model=RecipientOut, status_code=status.HTTP_201_CREATED, summary="Add a recipient assignment")
def create_recipient(body: RecipientIn, db: Session = Depends(get_db), actor: Actor = Depends(get_current_actor)) -> NotificationRecipient:
    _needs_a_person(body)
    row = config_store.add_recipient(db, **body.model_dump(), actor=actor.id)
    db.commit()
    return row


@router.post("/recipients/{recipient_id}/update", response_model=RecipientOut, summary="Change a recipient assignment (set active=false to retire it)")
def change_recipient(
    recipient_id: int, body: RecipientIn, db: Session = Depends(get_db), actor: Actor = Depends(get_current_actor)
) -> NotificationRecipient:
    _needs_a_person(body)
    row = _recipient_or_404(db, recipient_id)
    row.role = body.role
    config_store.update_recipient(db, row, plant=body.plant, user_oid=body.user_oid, email=body.email, active=body.active, actor=actor.id)
    db.commit()
    return row


# --- SLA (reminder and escalation timings) -----------------------------------


class SlaIn(BaseModel):
    approval_route: str = Field(description="OAR, ROP_MAX or ALL")
    role: str | None = Field(default=None, description="Empty means every role on the route.")
    first_reminder_minutes: int = Field(ge=1, description="How long an approver waits before the first reminder.")
    repeat_minutes: int = Field(ge=1, description="Gap between later reminders.")
    max_reminders: int = Field(ge=0, description="0 sends no reminders.")
    escalate_after_minutes: int | None = Field(default=None, ge=1, description="Empty means never escalate.")
    escalation_role: str | None = Field(default=None, description="Who is told when it escalates.")
    active: bool = True

    _check_role = field_validator("role", "escalation_role")(_known_role)

    @field_validator("approval_route")
    @classmethod
    def _check_route(cls, value: str) -> str:
        if value not in APPROVAL_ROUTES:
            raise ValueError(f"approval_route must be one of {', '.join(APPROVAL_ROUTES)}")
        return value


class SlaOut(SlaIn):
    id: int
    updated_at: datetime | None = None
    updated_by: str | None = None

    model_config = {"from_attributes": True}


def _sla_or_404(db: Session, sla_id: int) -> NotificationSla:
    row = db.get(NotificationSla, sla_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SLA not found")
    return row


def _check_sla(db: Session, body: SlaIn, own_id: int | None = None) -> None:
    if (body.escalate_after_minutes is None) != (body.escalation_role is None):
        raise HTTPException(422, "Give both escalate_after_minutes and escalation_role, or neither.")
    clash = config_store.find_sla(db, body.approval_route, body.role)
    if clash is not None and clash.id != own_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "An SLA for that route and role already exists; change it instead.")


@router.get("/sla", response_model=list[SlaOut], summary="List reminder and escalation timings")
def list_sla(db: Session = Depends(get_db)) -> list[NotificationSla]:
    return config_store.list_sla(db)


@router.post("/sla", response_model=SlaOut, status_code=status.HTTP_201_CREATED, summary="Add reminder and escalation timings")
def create_sla(body: SlaIn, db: Session = Depends(get_db), actor: Actor = Depends(get_current_actor)) -> NotificationSla:
    _check_sla(db, body)
    row = config_store.add_sla(db, actor=actor.id, **body.model_dump())
    db.commit()
    return row


@router.post("/sla/{sla_id}/update", response_model=SlaOut, summary="Change reminder and escalation timings (set active=false to switch them off)")
def change_sla(sla_id: int, body: SlaIn, db: Session = Depends(get_db), actor: Actor = Depends(get_current_actor)) -> NotificationSla:
    row = _sla_or_404(db, sla_id)
    _check_sla(db, body, own_id=row.id)
    config_store.update_sla(db, row, actor=actor.id, **body.model_dump())
    db.commit()
    return row

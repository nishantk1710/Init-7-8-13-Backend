"""The signed-in user's notification inbox.

Every query is limited to in-app notifications addressed to the caller, so one
person can never read or change another's, whatever id they ask for -- another
person's id answers 404, the same as an id that does not exist. The caller is the
signed-in user's Entra object id (the ``X-Actor-Id`` header only when sign-in is
off), never a value from the request.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_current_actor
from app.core.db import get_db
from app.core.security import USER_ROLES, require_roles
from app.integrations.notifications.models import Channel
from app.models.notification import NotificationRow

router = APIRouter(prefix="/notifications", tags=["notifications"])

MAX_PAGE_SIZE = 100

#: Marking read changes only the caller's own rows, so any signed-in user with a role may.
ANY_USER = Depends(require_roles(*sorted(USER_ROLES)))


class _Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


class InboxItem(_Camel):
    """One notification as the person sees it. The internal module name is not included."""

    id: str
    title: str
    description: str
    event_type: str
    entity_type: str | None = None
    entity_id: str | None = None
    plant: str | None = None
    action_path: str | None = None
    is_read: bool
    created_at: datetime
    read_at: datetime | None = None


class InboxPage(_Camel):
    items: list[InboxItem]
    total: int
    page: int
    page_size: int
    unread_count: int


class UnreadCount(_Camel):
    unread: int


class MarkedRead(_Camel):
    updated: int


def _mine(actor: Actor):
    return (NotificationRow.channel == Channel.IN_APP.value, NotificationRow.recipient_user_id == actor.id)


def _own_or_404(db: Session, actor: Actor, notification_id: str) -> NotificationRow:
    row = db.scalars(select(NotificationRow).where(NotificationRow.id == notification_id, *_mine(actor))).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    return row


def _unread(db: Session, actor: Actor) -> int:
    return db.scalar(select(func.count()).select_from(NotificationRow).where(*_mine(actor), ~NotificationRow.is_read)) or 0


@router.get("", response_model=InboxPage, summary="My notifications, newest first")
def list_my_notifications(
    status_filter: Literal["all", "unread", "read"] = Query("all", alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, alias="pageSize", ge=1),
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> InboxPage:
    size = min(page_size, MAX_PAGE_SIZE)
    conditions = list(_mine(actor))
    if status_filter == "unread":
        conditions.append(~NotificationRow.is_read)
    elif status_filter == "read":
        conditions.append(NotificationRow.is_read)

    total = db.scalar(select(func.count()).select_from(NotificationRow).where(*conditions)) or 0
    rows = db.scalars(
        select(NotificationRow)
        .where(*conditions)
        .order_by(NotificationRow.created_at.desc(), NotificationRow.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    return InboxPage(
        items=[InboxItem.model_validate(row) for row in rows],
        total=total,
        page=page,
        page_size=size,
        unread_count=_unread(db, actor),
    )


@router.get("/unread-count", response_model=UnreadCount, summary="How many of my notifications are unread")
def unread_count(db: Session = Depends(get_db), actor: Actor = Depends(get_current_actor)) -> UnreadCount:
    return UnreadCount(unread=_unread(db, actor))


@router.post("/read-all", response_model=MarkedRead, dependencies=[ANY_USER], summary="Mark all of my notifications as read")
def mark_all_read(db: Session = Depends(get_db), actor: Actor = Depends(get_current_actor)) -> MarkedRead:
    result = db.execute(
        update(NotificationRow)
        .where(*_mine(actor), ~NotificationRow.is_read)
        .values(is_read=True, read_at=datetime.now(timezone.utc))
    )
    db.commit()
    return MarkedRead(updated=result.rowcount or 0)


@router.get("/{notification_id}", response_model=InboxItem, summary="One of my notifications")
def get_my_notification(notification_id: str, db: Session = Depends(get_db), actor: Actor = Depends(get_current_actor)) -> NotificationRow:
    return _own_or_404(db, actor, notification_id)


@router.post("/{notification_id}/read", response_model=InboxItem, dependencies=[ANY_USER], summary="Mark one of my notifications as read")
def mark_read(notification_id: str, db: Session = Depends(get_db), actor: Actor = Depends(get_current_actor)) -> NotificationRow:
    row = _own_or_404(db, actor, notification_id)
    if not row.is_read:
        row.is_read = True
        row.read_at = datetime.now(timezone.utc)
        db.commit()
    return row

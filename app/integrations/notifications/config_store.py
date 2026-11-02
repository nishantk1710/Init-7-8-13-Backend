"""Reading and editing the recipient and SLA tables, with an audit row per change.

Used by the administrator API. Nothing here commits: the caller owns the
transaction, so a change and its audit row are saved together or not at all.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.notification import NotificationConfigAudit, NotificationRecipient, NotificationSla

RECIPIENTS_TABLE = "notification_recipients"
SLA_TABLE = "notification_sla"


def record_audit(db: Session, table_name: str, row_id: int, action: str, actor: str | None, detail: str) -> None:
    db.add(NotificationConfigAudit(table_name=table_name, row_id=row_id, action=action, actor=actor, detail=detail))


def describe(row: object, fields: tuple[str, ...]) -> str:
    return ", ".join(f"{field}={getattr(row, field)!r}" for field in fields)


_RECIPIENT_FIELDS = ("plant", "role", "user_oid", "email", "active")


def list_recipients(db: Session) -> list[NotificationRecipient]:
    return list(db.scalars(select(NotificationRecipient).order_by(NotificationRecipient.role, NotificationRecipient.plant, NotificationRecipient.id)))


def add_recipient(
    db: Session, *, role: str, plant: str | None, user_oid: str | None, email: str | None, active: bool, actor: str
) -> NotificationRecipient:
    row = NotificationRecipient(
        role=role, plant=plant or None, user_oid=user_oid or None, email=email or None, active=active,
        created_by=actor, updated_by=actor,
    )
    db.add(row)
    db.flush()
    record_audit(db, RECIPIENTS_TABLE, row.id, "CREATE", actor, describe(row, _RECIPIENT_FIELDS))
    return row


def update_recipient(
    db: Session, row: NotificationRecipient, *, plant: str | None, user_oid: str | None, email: str | None, active: bool, actor: str
) -> NotificationRecipient:
    row.plant = plant or None
    row.user_oid = user_oid or None
    row.email = email or None
    row.active = active
    row.updated_by = actor
    db.flush()
    record_audit(db, RECIPIENTS_TABLE, row.id, "UPDATE", actor, describe(row, _RECIPIENT_FIELDS))
    return row



_SLA_FIELDS = (
    "approval_route",
    "role",
    "first_reminder_minutes",
    "repeat_minutes",
    "max_reminders",
    "escalate_after_minutes",
    "escalation_role",
    "active",
)


def list_sla(db: Session) -> list[NotificationSla]:
    return list(db.scalars(select(NotificationSla).order_by(NotificationSla.approval_route, NotificationSla.role, NotificationSla.id)))


def find_sla(db: Session, approval_route: str, role: str | None) -> NotificationSla | None:
    """The row for exactly this route and role (``None`` role = the route's default row)."""
    return db.scalars(
        select(NotificationSla).where(
            NotificationSla.approval_route == approval_route,
            NotificationSla.role.is_(None) if role is None else NotificationSla.role == role,
        )
    ).first()


def add_sla(db: Session, *, actor: str, **fields) -> NotificationSla:
    row = NotificationSla(**fields, created_by=actor, updated_by=actor)
    db.add(row)
    db.flush()
    record_audit(db, SLA_TABLE, row.id, "CREATE", actor, describe(row, _SLA_FIELDS))
    return row


def update_sla(db: Session, row: NotificationSla, *, actor: str, **fields) -> NotificationSla:
    for name, value in fields.items():
        setattr(row, name, value)
    row.updated_by = actor
    db.flush()
    record_audit(db, SLA_TABLE, row.id, "UPDATE", actor, describe(row, _SLA_FIELDS))
    return row



def active_sla_for(db: Session, approval_route: str, role: str) -> NotificationSla | None:
    """The reminder timings that apply to ``role`` on ``approval_route``, most specific first.

    Order: this route and role, this route, any route and this role, any route.
    ``None`` -- no active row -- means no reminders and no escalation.
    """
    rows = list(db.scalars(select(NotificationSla).where(NotificationSla.active)))
    for route, wanted_role in ((approval_route, role), (approval_route, None), ("ALL", role), ("ALL", None)):
        for row in rows:
            if row.approval_route == route and row.role == wanted_role:
                return row
    return None

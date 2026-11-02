"""The shared notification tables, used by every module.

* ``notification`` -- one row per message per recipient per channel. It is both
  the inbox (``channel = IN_APP``) and the outbox the email worker reads
  (``channel = EMAIL``). ``module`` is internal bookkeeping and is never shown.
* ``notification_recipients`` -- who holds which approval role at which plant.
  Maintained by an administrator, so an approver can change without a deploy.
* ``notification_sla`` -- reminder and escalation timings. With no active row
  nothing is reminded or escalated.
* ``notification_reminder_state`` -- how many reminders an approval stage has
  had, so a restart never repeats one.
* ``notification_config_audit`` -- append-only history of recipient and SLA edits.

The older ``i8_notification`` and ``i13_act_notification`` tables are untouched.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


def _new_id() -> str:
    return str(uuid.uuid4())


class NotificationRow(Base):
    __tablename__ = "notification"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    # Stops the same message being created twice, e.g. when a request is retried.
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    module: Mapped[str] = mapped_column(String(16), nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)

    recipient_user_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    recipient_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    recipient_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plant: Mapped[str | None] = mapped_column(String(8), nullable=True)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    action_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class NotificationRecipient(Base):
    __tablename__ = "notification_recipients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Empty plant means "every plant".
    plant: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # The Entra app-role value, e.g. ``Spares.EngineeringHead``.
    role: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # The Entra object id. The email comes from ``app_user`` when it is known.
    user_oid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Only for a person who has not signed in yet, so ``app_user`` has no email.
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    created_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(40), nullable=True)


class NotificationSla(Base):
    __tablename__ = "notification_sla"
    __table_args__ = (UniqueConstraint("approval_route", "role", name="uq_notification_sla_route_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # ``OAR``, ``ROP_MAX`` or ``ALL``.
    approval_route: Mapped[str] = mapped_column(String(16), nullable=False)
    # Empty role means "every role on that route".
    role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_reminder_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    repeat_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    max_reminders: Mapped[int] = mapped_column(Integer, nullable=False)
    # Empty means this row never escalates.
    escalate_after_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    escalation_role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    created_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(40), nullable=True)


class NotificationReminderState(Base):
    """One row per approval stage a recommendation has waited in.

    ``pending_since_ledger_id`` is the ledger entry that put it in that stage, so
    when the recommendation moves on the key changes and the count starts again.
    """

    __tablename__ = "notification_reminder_state"
    __table_args__ = (
        UniqueConstraint("recommendation_id", "pending_since_ledger_id", name="uq_notification_reminder_stage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recommendation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pending_since_ledger_id: Mapped[int] = mapped_column(Integer, nullable=False)
    reminders_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationConfigAudit(Base):
    """Append-only: who changed which recipient or SLA row, and how."""

    __tablename__ = "notification_config_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String(48), nullable=False)
    row_id: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # A short text copy of the row after the change (before it, for a delete).
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )

"""W6.6: persisted ACT exceptions, their append-only audit trail, requester
confirmations and notification attempts.

Unlike the W6.3/W6.5 marts (``i13_watch_metric_mart``,
``i13_reclassification`` -- both grain (material, plant), refreshed by a
portable delete-then-insert), an ACT exception has identity and a state
machine that must survive across detection runs: an operator/requester acts
on one specific exception, and a later ``detect_exceptions`` run must not
wipe that state. So ``i13_act_exception`` is upserted by its deterministic
``exception_id`` business key (see
``app.initiatives.i13.act.service``'s module docstring for exactly how that
key is built and why re-running detection is idempotent against it), never
deleted-and-reinserted.

Portable constructs only (no ``JSONB``/``ARRAY``) -- same cross-dialect
(Postgres/Azure SQL) rule as every other I13 model (see
``app/models/base.py``). ``evidence``/``metadata`` are stored as JSON text in
a plain ``String``/``Text`` column (``json.dumps``/``json.loads`` at the
persistence-adapter boundary in
``app.initiatives.i13.act_exception_store``), never a native JSON column
type.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ActExceptionRecord(Base):
    __tablename__ = "i13_act_exception"

    exception_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    exception_type: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)

    material: Mapped[str] = mapped_column(String(40), index=True)
    plant: Mapped[str] = mapped_column(String(10), index=True)

    reservation_number: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    reservation_item: Mapped[str | None] = mapped_column(String(10), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ledger_entry_id: Mapped[str | None] = mapped_column(String(80), nullable=True)

    owner_requester_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requester_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    current_assignee_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    current_assignee_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    routing_status: Mapped[str | None] = mapped_column(String(24), nullable=True)

    reason: Mapped[str] = mapped_column(String(400))
    evidence_json: Mapped[str] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<ActExceptionRecord {self.exception_id} type={self.exception_type} status={self.status}>"


class ActExceptionEventRecord(Base):
    """Append-only -- no update/delete path exists anywhere in this
    codebase for this table. Every ACT state change, routing attempt,
    confirmation and notification attempt is a new row here."""

    __tablename__ = "i13_act_exception_event"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exception_id: Mapped[str] = mapped_column(
        String(120), ForeignKey("i13_act_exception.exception_id"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(16))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    metadata_json: Mapped[str] = mapped_column(Text)

    def __repr__(self) -> str:
        return f"<ActExceptionEventRecord {self.exception_id} {self.event_type} {self.from_status}->{self.to_status}>"


class ActConfirmationRecord(Base):
    __tablename__ = "i13_act_confirmation"

    confirmation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exception_id: Mapped[str] = mapped_column(
        String(120), ForeignKey("i13_act_exception.exception_id"), index=True
    )
    reason_category: Mapped[str] = mapped_column(String(60))
    free_text: Mapped[str] = mapped_column(String(2000))
    actor_id: Mapped[str] = mapped_column(String(40))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<ActConfirmationRecord {self.exception_id} by={self.actor_id}>"


class ActNotificationRecord(Base):
    __tablename__ = "i13_act_notification"

    notification_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exception_id: Mapped[str] = mapped_column(
        String(120), ForeignKey("i13_act_exception.exception_id"), index=True
    )
    channel: Mapped[str] = mapped_column(String(16))
    recipient: Mapped[str | None] = mapped_column(String(120), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), index=True)
    detail: Mapped[str] = mapped_column(String(400))
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<ActNotificationRecord {self.exception_id} channel={self.channel} outcome={self.outcome}>"

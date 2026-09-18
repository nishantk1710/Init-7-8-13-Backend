"""W6.6: the Postgres/Azure-SQL-portable ``ExceptionRepository`` adapter
(``app.initiatives.i13.act.ports.ExceptionRepository``).

The one place in this feature that imports SQLAlchemy -- ``app.initiatives
.i13.act`` (detection, state machine, application services) never does, by
design (see that package's ``__init__.py``). Swapping Postgres for Azure SQL
later is a change to *this* module (and ``app/core/db.py``, already
dialect-agnostic) -- ``app.initiatives.i13.act.service`` does not change.

Upserts by ``exception_id`` (get-then-insert-or-update), never a delete-
then-insert mart refresh: see ``app.models.i13_act_exception``'s docstring
for why an exception's persisted state must survive across detection runs.
Events/confirmations/notifications are pure inserts -- append-only.

``evidence``/``metadata`` are stored as JSON text (see
``app.models.i13_act_exception``'s portability note) -- serialised/
deserialised only here, at the boundary between the domain dataclasses and
the ORM rows.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.initiatives.i13.act.domain import (
    ActException,
    AssigneeType,
    EventType,
    ExceptionEvent,
    ExceptionStatus,
    ExceptionType,
    NotificationAttempt,
    NotificationChannel,
    NotificationOutcome,
    RequesterConfirmation,
    RoutingStatus,
)
from app.models.i13_act_exception import (
    ActConfirmationRecord,
    ActExceptionEventRecord,
    ActExceptionRecord,
    ActNotificationRecord,
)


def _exception_to_row(exception: ActException, existing: ActExceptionRecord | None) -> ActExceptionRecord:
    row = existing or ActExceptionRecord(exception_id=exception.exception_id)
    row.exception_type = exception.exception_type.value
    row.status = exception.status.value
    row.material = exception.material
    row.plant = exception.plant
    row.reservation_number = exception.reservation_number
    row.reservation_item = exception.reservation_item
    row.session_id = exception.session_id
    row.ledger_entry_id = exception.ledger_entry_id
    row.owner_requester_id = exception.owner_requester_id
    row.detected_at = exception.detected_at
    row.requester_due_at = exception.requester_due_at
    row.escalated_at = exception.escalated_at
    row.resolved_at = exception.resolved_at
    row.current_assignee_type = exception.current_assignee_type.value if exception.current_assignee_type else None
    row.current_assignee_id = exception.current_assignee_id
    row.routing_status = exception.routing_status.value if exception.routing_status else None
    row.reason = exception.reason
    row.evidence_json = json.dumps(exception.evidence)
    return row


def _row_to_exception(row: ActExceptionRecord) -> ActException:
    return ActException(
        exception_id=row.exception_id,
        exception_type=ExceptionType(row.exception_type),
        status=ExceptionStatus(row.status),
        material=row.material,
        plant=row.plant,
        reservation_number=row.reservation_number,
        reservation_item=row.reservation_item,
        session_id=row.session_id,
        ledger_entry_id=row.ledger_entry_id,
        owner_requester_id=row.owner_requester_id,
        detected_at=row.detected_at,
        requester_due_at=row.requester_due_at,
        escalated_at=row.escalated_at,
        resolved_at=row.resolved_at,
        current_assignee_type=AssigneeType(row.current_assignee_type) if row.current_assignee_type else None,
        current_assignee_id=row.current_assignee_id,
        routing_status=RoutingStatus(row.routing_status) if row.routing_status else None,
        reason=row.reason,
        evidence=json.loads(row.evidence_json) if row.evidence_json else {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlExceptionRepository:
    """SQLAlchemy-backed ``ExceptionRepository``. The caller owns the
    transaction boundary (this codebase's convention -- see
    ``watch_mart.py``'s docstring): this class flushes but never commits."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, exception_id: str) -> ActException | None:
        row = self._session.get(ActExceptionRecord, exception_id)
        return _row_to_exception(row) if row else None

    def upsert(self, exception: ActException) -> None:
        existing = self._session.get(ActExceptionRecord, exception.exception_id)
        row = _exception_to_row(exception, existing)
        if existing is None:
            self._session.add(row)
        self._session.flush()

    def list(
        self,
        *,
        material: str | None = None,
        plant: str | None = None,
        exception_type: ExceptionType | None = None,
        status: ExceptionStatus | None = None,
        owner_requester_id: str | None = None,
    ) -> list[ActException]:
        stmt = select(ActExceptionRecord)
        if material:
            stmt = stmt.where(ActExceptionRecord.material == material)
        if plant:
            stmt = stmt.where(ActExceptionRecord.plant == plant)
        if exception_type:
            stmt = stmt.where(ActExceptionRecord.exception_type == exception_type.value)
        if status:
            stmt = stmt.where(ActExceptionRecord.status == status.value)
        if owner_requester_id:
            stmt = stmt.where(ActExceptionRecord.owner_requester_id == owner_requester_id)
        rows = self._session.execute(stmt.order_by(ActExceptionRecord.detected_at.desc())).scalars().all()
        return [_row_to_exception(row) for row in rows]

    def append_event(self, event: ExceptionEvent) -> None:
        self._session.add(
            ActExceptionEventRecord(
                exception_id=event.exception_id,
                event_type=event.event_type.value,
                from_status=event.from_status.value if event.from_status else None,
                to_status=event.to_status.value if event.to_status else None,
                actor_id=event.actor_id,
                actor_type=event.actor_type,
                timestamp=event.timestamp,
                metadata_json=json.dumps(event.metadata),
            )
        )
        self._session.flush()

    def list_events(self, exception_id: str) -> list[ExceptionEvent]:
        stmt = (
            select(ActExceptionEventRecord)
            .where(ActExceptionEventRecord.exception_id == exception_id)
            .order_by(ActExceptionEventRecord.timestamp.asc(), ActExceptionEventRecord.event_id.asc())
        )
        rows = self._session.execute(stmt).scalars().all()
        return [
            ExceptionEvent(
                exception_id=row.exception_id,
                event_type=EventType(row.event_type),
                from_status=ExceptionStatus(row.from_status) if row.from_status else None,
                to_status=ExceptionStatus(row.to_status) if row.to_status else None,
                actor_id=row.actor_id,
                actor_type=row.actor_type,
                timestamp=row.timestamp,
                metadata=json.loads(row.metadata_json) if row.metadata_json else {},
                event_id=str(row.event_id),
            )
            for row in rows
        ]

    def save_confirmation(self, confirmation: RequesterConfirmation) -> None:
        self._session.add(
            ActConfirmationRecord(
                exception_id=confirmation.exception_id,
                reason_category=confirmation.reason_category,
                free_text=confirmation.free_text,
                actor_id=confirmation.actor_id,
                submitted_at=confirmation.submitted_at,
            )
        )
        self._session.flush()

    def get_confirmation(self, exception_id: str) -> RequesterConfirmation | None:
        stmt = (
            select(ActConfirmationRecord)
            .where(ActConfirmationRecord.exception_id == exception_id)
            .order_by(ActConfirmationRecord.submitted_at.desc())
            .limit(1)
        )
        row = self._session.execute(stmt).scalars().first()
        if row is None:
            return None
        return RequesterConfirmation(
            exception_id=row.exception_id,
            reason_category=row.reason_category,
            free_text=row.free_text,
            actor_id=row.actor_id,
            submitted_at=row.submitted_at,
            confirmation_id=str(row.confirmation_id),
        )

    def record_notification(self, attempt: NotificationAttempt) -> None:
        self._session.add(
            ActNotificationRecord(
                exception_id=attempt.exception_id,
                channel=attempt.channel.value,
                recipient=attempt.recipient,
                outcome=attempt.outcome.value,
                detail=attempt.detail,
                attempted_at=attempt.attempted_at,
            )
        )
        self._session.flush()

    def list_notifications(self, exception_id: str) -> list[NotificationAttempt]:
        stmt = (
            select(ActNotificationRecord)
            .where(ActNotificationRecord.exception_id == exception_id)
            .order_by(ActNotificationRecord.attempted_at.asc())
        )
        rows = self._session.execute(stmt).scalars().all()
        return [
            NotificationAttempt(
                exception_id=row.exception_id,
                channel=NotificationChannel(row.channel),
                recipient=row.recipient,
                outcome=NotificationOutcome(row.outcome),
                detail=row.detail,
                attempted_at=row.attempted_at,
                notification_id=str(row.notification_id),
            )
            for row in rows
        ]

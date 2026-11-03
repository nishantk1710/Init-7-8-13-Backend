"""Telling a requester about their request's SAP reservation: linked, or still not.

Run on a timer by the notification scheduler. Two notices, both in-app and both
to the request's requester only:

* **Linked** -- one or more of the request's lines were linked to an SAP
  reservation since the last notice for that request. Lines linked in the same
  pass are one notice, not one each. Whatever made the link (the hourly
  snapshot rebuild, the "Link reservation" button, UAT) is not this module's
  concern: it reads ``session_reservation_link`` afterwards, so no linking code
  has to know about notifications.
* **Reminder** -- once, when a request is ``ASSISTANT_LINK_REMINDER_HOURS`` old
  and a line that needs a reservation (``proceed`` / ``proceed_new``) has none.
  A line marked not needed, or that uses the existing unit, is never reminded.

No state table. What was already said is read from the ``notification`` rows
themselves: a link is new when it was first seen after the request's latest
"linked" notice, and the reminder's idempotency key names the request, so a
repeat pass, a second worker or a restart adds nothing. Both look back only
``ASSISTANT_LINK_NOTICE_LOOKBACK_HOURS``, so switching this on does not notify
anybody about last month's requests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.assistant import requests as request_service
from app.assistant import script
from app.assistant.models import AssistantRequest, AssistantSession
from app.core.config import get_settings
from app.integrations.notifications import templates
from app.integrations.notifications.models import Channel, NotificationEvent
from app.integrations.notifications.resolver import resolve_user
from app.integrations.notifications.service import enqueue
from app.models.i13_session_link import SessionReservationLink
from app.models.notification import NotificationRow

logger = logging.getLogger(__name__)

#: Not an initiative name: a request holds both kinds of line, and "I08" notices are hidden from the inbox.
MODULE = "ASSISTANT"
ENTITY = "assistant_request"
#: The decisions that end in a reservation. The others (not needed, use the existing unit) expect none.
NEEDS_RESERVATION = (script.PROCEED, script.PROCEED_NEW)
_IN_APP = (Channel.IN_APP,)


@dataclass(frozen=True)
class LinkNoticeResult:
    linked: int = 0
    reminders: int = 0


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def request_path(request_id: str) -> str:
    return f"/assistant/requests/{request_id}"


def _send(db: Session, request: AssistantRequest, event_type: str, trigger_id: str, context: dict[str, str]) -> int:
    event = NotificationEvent(
        module=MODULE,
        event_type=event_type,
        entity_type=ENTITY,
        entity_id=request.id,
        trigger_id=trigger_id,
        action_path=request_path(request.id),
        plant=request.plant,
        context={"request": request.id, **context},
    )
    created = enqueue(db, event, resolve_user(db, request.requester, plant=request.plant), channels=_IN_APP)
    return sum(1 for row in created if row.channel == Channel.IN_APP.value and row.recipient_user_id)


# --- linked -------------------------------------------------------------------------


def _last_linked_notice(db: Session, request_ids: list[str]) -> dict[str, datetime]:
    """When each request was last told about a link -- any row, failed ones too, so a failure is not repeated."""
    return {
        entity_id: _aware(latest)
        for entity_id, latest in db.execute(
            select(NotificationRow.entity_id, func.max(NotificationRow.created_at))
            .where(
                NotificationRow.event_type == templates.RESERVATION_LINKED,
                NotificationRow.entity_id.in_(request_ids),
            )
            .group_by(NotificationRow.entity_id)
        ).all()
    }


def notify_new_links(db: Session, now: datetime, lookback: timedelta) -> int:
    """One "linked" notice per request with links first seen since its last notice."""
    rows = db.execute(
        select(SessionReservationLink, AssistantSession.request_id, AssistantSession.line_no)
        .join(AssistantSession, AssistantSession.id == SessionReservationLink.session_id)
        .where(AssistantSession.request_id.is_not(None), SessionReservationLink.first_seen_at >= now - lookback)
    ).all()
    by_request: dict[str, list[tuple[SessionReservationLink, int | None]]] = {}
    for link, request_id, line_no in rows:
        by_request.setdefault(request_id, []).append((link, line_no))
    if not by_request:
        return 0

    last = _last_linked_notice(db, list(by_request))
    sent = 0
    for request_id, links in sorted(by_request.items()):
        since = last.get(request_id)
        new = [(l, n) for l, n in links if since is None or _aware(l.first_seen_at) > since]
        request = db.get(AssistantRequest, request_id) if new else None
        if request is None:
            continue
        new.sort(key=lambda pair: (pair[1] or 0, pair[0].reservation_number, pair[0].reservation_item))
        sessions = request_service.lines_of(db, request_id)
        linked_sessions = set(
            db.scalars(
                select(SessionReservationLink.session_id)
                .where(SessionReservationLink.session_id.in_([s.id for s in sessions]))
                .distinct()
            )
        )
        lines = "\n".join(
            f"Line {line_no or '-'} – Material {link.material}: reservation {link.reservation_number} "
            f"item {link.reservation_item}"
            for link, line_no in new
        )
        try:
            sent += _send(
                db,
                request,
                templates.RESERVATION_LINKED,
                f"link-{max(link.id for link, _ in new)}",
                {"lines": lines, "linked": str(len(linked_sessions)), "total": str(len(sessions))},
            )
        except Exception:  # noqa: BLE001 -- one request must not stop the others
            logger.exception("Request link notice: could not notify for %s", request_id)
    return sent


# --- reminder -----------------------------------------------------------------------


def _already_reminded(db: Session, request_ids: list[str]) -> set[str]:
    return set(
        db.scalars(
            select(NotificationRow.entity_id)
            .where(
                NotificationRow.event_type == templates.RESERVATION_LINK_REMINDER,
                NotificationRow.entity_id.in_(request_ids),
            )
            .distinct()
        )
    )


def remind_unlinked(db: Session, now: datetime, after: timedelta, lookback: timedelta) -> int:
    """One reminder per request that is ``after`` old and still has a line waiting for its reservation."""
    due = list(
        db.scalars(
            select(AssistantRequest)
            .where(AssistantRequest.issued_at <= now - after, AssistantRequest.issued_at > now - after - lookback)
            .order_by(AssistantRequest.issued_at)
        )
    )
    if not due:
        return 0
    reminded = _already_reminded(db, [r.id for r in due])
    sent = 0
    for request in due:
        if request.id in reminded:
            continue
        try:
            sessions = request_service.lines_of(db, request.id)
            linked = set(
                db.scalars(
                    select(SessionReservationLink.session_id)
                    .where(SessionReservationLink.session_id.in_([s.id for s in sessions]))
                    .distinct()
                )
            ) if sessions else set()
            if all(s.id in linked for s in sessions):
                continue
            waiting = [
                record
                for record in request_service.line_records(db, [s for s in sessions if s.id not in linked])
                if record.decision in NEEDS_RESERVATION
            ]
            if not waiting:
                continue
            lines = "\n".join(
                f"Line {record.session.line_no or '-'} – Material {record.session.material_id}" for record in waiting
            )
            sent += _send(db, request, templates.RESERVATION_LINK_REMINDER, "reminder-1", {"lines": lines})
        except Exception:  # noqa: BLE001 -- one request must not stop the others
            logger.exception("Request link reminder: could not check %s", request.id)
    return sent


# --- the job ------------------------------------------------------------------------


def run_link_notices(db: Session, now: datetime | None = None, settings=None) -> LinkNoticeResult:
    """One pass of both. Queues the notices; the caller commits."""
    settings = settings or get_settings()
    if not settings.assistant_link_notifications_enabled:
        return LinkNoticeResult()
    now = _aware(now or datetime.now(timezone.utc))
    lookback = timedelta(hours=max(settings.assistant_link_notice_lookback_hours, 1))
    linked = notify_new_links(db, now, lookback)
    reminders = 0
    if settings.assistant_link_reminder_hours > 0:
        reminders = remind_unlinked(db, now, timedelta(hours=settings.assistant_link_reminder_hours), lookback)
    return LinkNoticeResult(linked=linked, reminders=reminders)


def run_job(settings=None, *, session_factory=None, now: datetime | None = None) -> str:
    settings = settings or get_settings()
    if not settings.assistant_link_notifications_enabled:
        return "disabled (ASSISTANT_LINK_NOTIFICATIONS_ENABLED is false)"
    if session_factory is None:
        from app.core.db import get_sessionmaker

        session_factory = get_sessionmaker()
    with session_factory() as db:
        result = run_link_notices(db, now, settings)
        db.commit()
    return f"{result.linked} linked notice(s), {result.reminders} reminder(s)"

"""Telling the people on a request about its SAP reservation: raised, linked, or still not.

Run on a timer by the notification scheduler, in-app only. Two people are on a request. The one who **raised** it
(the signed-in user) creates the reservation in SAP and links it. The one **named in its Requester box** is who the
parts are for. The box is free text, so it is not identity on its own: it stands for a person when it is listed in
``I13_REQUESTER_RECIPIENTS`` (the override), or matches exactly one person who has signed in to the app: their whole name
or e-mail, or two or more words of their name (see ``named_person``). Anything else is not notified, nobody is guessed. Someone who
is both is only the raiser, so they hear each thing once.

* **Raised** -- once, soon after the request, while a line that needs a reservation (``proceed`` / ``proceed_new``)
  has none linked. The raiser is told to create the reservation in SAP and link it; the named person is told who
  raised it for them and that it is waiting for linking.
* **Linked** -- one or more of the request's lines were linked to an SAP reservation since the last notice for that
  request, to **both**. Lines linked in the same pass are one notice, not one each. Whatever made the link (the
  hourly snapshot rebuild, the "Link reservation" button, UAT) is not this module's concern: it reads
  ``session_reservation_link`` afterwards, so no linking code has to know about notifications.
* **Reminder** -- once, when a request is ``ASSISTANT_LINK_REMINDER_HOURS`` old and such a line still has none, to
  **both**: the raiser to act, the named person to know it is still waiting. A line marked not needed, or that uses
  the existing unit, is never mentioned.

No state table. What was already said is read from the ``notification`` rows
themselves: a link is new when it was first seen after the request's latest
"linked" notice, and the reminder's idempotency key names the request, so a
repeat pass, a second worker or a restart adds nothing. Both look back only
``ASSISTANT_LINK_NOTICE_LOOKBACK_HOURS``, so switching this on does not notify
anybody about last month's requests.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
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
from app.core.user_directory import names_for, oid_for_name
from app.integrations.notifications.resolver import is_entra_object_id, resolve_user
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
    raised: int = 0


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def request_path(request_id: str) -> str:
    return f"/assistant/requests/{request_id}"


def named_person(db: Session, request: AssistantRequest, aliases: Mapping[str, str] | None) -> str | None:
    """The Entra object id of the person named in the Requester box, or ``None``.

    ``requested_for`` is free text, never identity on its own. It stands for a person in two ways, in this order:

    1. An administrator listed the name in ``I13_REQUESTER_RECIPIENTS`` (the list the exception notices use) -- the
       override, and the only way to reach a name that is not the one the person signs in with.
    2. It matches exactly one person who has signed in to the app (``user_directory.oid_for_name``): the whole name or
       e-mail address they signed in with, or at least two typed words that are all whole words of their name, in
       any order -- "Asha Rao" finds "Asha Meera Rao". Case and spaces never matter.

    Anything else -- no match, or two people with that name -- is not notified: nobody is guessed. Only people who
    have signed in are known, so a person who never has has no object id here to notify.
    """
    name = (request.requested_for or "").strip()
    if not name:
        return None
    return (aliases or {}).get(name.lower()) or oid_for_name(db, name)


def _audiences(db: Session, request: AssistantRequest, aliases: Mapping[str, str] | None):
    """``(the person who raised the request, the person named in its Requester box)``, each a list of recipients.

    The raiser is the one who creates the reservation in SAP and links it. The named person is who the parts are for.
    When they are the same person that person is only the raiser, so nobody hears the same thing twice.
    """
    raiser = list(resolve_user(db, request.requester, plant=request.plant))
    named_id = named_person(db, request, aliases)
    named = list(resolve_user(db, named_id, plant=request.plant)) if named_id else []
    taken = {r.user_oid for r in raiser if r.user_oid}
    return raiser, [r for r in named if not (r.user_oid and r.user_oid in taken)]


def _raised_by(db: Session, request: AssistantRequest) -> str:
    """The raiser's name as a person reads it: from the user directory, else what was recorded (a typed name)."""
    who = (request.requester or "").strip()
    if is_entra_object_id(who):
        known = names_for(db, [who]).get(who)
        return known[0] if known and known[0] else "the person who raised it"
    return who or "the person who raised it"


def _context(db: Session, request: AssistantRequest, **extra: str) -> dict[str, str]:
    return {
        "requested_for": (request.requested_for or "").strip() or "yourself",
        "raised_by": _raised_by(db, request),
        **extra,
    }


def _send(
    db: Session, request: AssistantRequest, event_type: str, trigger_id: str, context: dict[str, str], recipients
) -> int:
    if not recipients:
        return 0
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
    created = enqueue(db, event, recipients, channels=_IN_APP)
    return sum(1 for row in created if row.channel == Channel.IN_APP.value and row.recipient_user_id)


def _already_told(db: Session, event_type: str, request_ids: list[str]) -> set[str]:
    """The requests that already have a row of this type, failed ones included, so a failure is not repeated."""
    return set(
        db.scalars(
            select(NotificationRow.entity_id)
            .where(NotificationRow.event_type == event_type, NotificationRow.entity_id.in_(request_ids))
            .distinct()
        )
    )


def _waiting_lines(db: Session, request: AssistantRequest):
    """The request's lines that need an SAP reservation and have none linked yet."""
    sessions = request_service.lines_of(db, request.id)
    if not sessions:
        return []
    linked = set(
        db.scalars(
            select(SessionReservationLink.session_id)
            .where(SessionReservationLink.session_id.in_([s.id for s in sessions]))
            .distinct()
        )
    )
    return [
        record
        for record in request_service.line_records(db, [s for s in sessions if s.id not in linked])
        if record.decision in NEEDS_RESERVATION
    ]


def _line_text(waiting) -> str:
    return "\n".join(f"Line {record.session.line_no or '-'} – Material {record.session.material_id}" for record in waiting)


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


def notify_new_links(db: Session, now: datetime, lookback: timedelta, aliases: Mapping[str, str] | None = None) -> int:
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
            raiser, named = _audiences(db, request, aliases)
            sent += _send(
                db,
                request,
                templates.RESERVATION_LINKED,
                f"link-{max(link.id for link, _ in new)}",
                {"lines": lines, "linked": str(len(linked_sessions)), "total": str(len(sessions))},
                raiser + named,
            )
        except Exception:  # noqa: BLE001 -- one request must not stop the others
            logger.exception("Request link notice: could not notify for %s", request_id)
    return sent


# --- raised -------------------------------------------------------------------------


def notify_raised(db: Session, now: datetime, lookback: timedelta, aliases: Mapping[str, str] | None = None) -> int:
    """Once per request, soon after it is raised, while a line that needs a reservation has none linked.

    The person who raised it is told to create the reservation in SAP and link it. The person named in its Requester
    box -- when that name is in the alias list -- is told who raised it for them and that it is waiting for linking.
    A request that needs no reservation, or was linked at once, says nothing.
    """
    candidates = list(
        db.scalars(
            select(AssistantRequest).where(AssistantRequest.issued_at > now - lookback).order_by(AssistantRequest.issued_at)
        )
    )
    if not candidates:
        return 0
    ids = [r.id for r in candidates]
    told_raiser = _already_told(db, templates.RESERVATION_LINK_REQUIRED, ids)
    told_named = _already_told(db, templates.RESERVATION_REQUESTED, ids)
    sent = 0
    for request in candidates:
        wants_named = request.id not in told_named and named_person(db, request, aliases) is not None
        if request.id in told_raiser and not wants_named:
            continue
        try:
            waiting = _waiting_lines(db, request)
            if not waiting:
                continue
            raiser, named = _audiences(db, request, aliases)
            context = _context(db, request, lines=_line_text(waiting))
            if request.id not in told_raiser:
                sent += _send(db, request, templates.RESERVATION_LINK_REQUIRED, "raised", context, raiser)
            if wants_named:
                sent += _send(db, request, templates.RESERVATION_REQUESTED, "raised", context, named)
        except Exception:  # noqa: BLE001 -- one request must not stop the others
            logger.exception("Request raised notice: could not notify for %s", request.id)
    return sent


# --- reminder -----------------------------------------------------------------------


def remind_unlinked(
    db: Session, now: datetime, after: timedelta, lookback: timedelta, aliases: Mapping[str, str] | None = None
) -> int:
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
    ids = [r.id for r in due]
    reminded_raiser = _already_told(db, templates.RESERVATION_LINK_REMINDER, ids)
    reminded_named = _already_told(db, templates.RESERVATION_LINK_PENDING, ids)
    sent = 0
    for request in due:
        wants_named = request.id not in reminded_named and named_person(db, request, aliases) is not None
        if request.id in reminded_raiser and not wants_named:
            continue
        try:
            waiting = _waiting_lines(db, request)
            if not waiting:
                continue
            raiser, named = _audiences(db, request, aliases)
            context = _context(db, request, lines=_line_text(waiting))
            if request.id not in reminded_raiser:
                sent += _send(db, request, templates.RESERVATION_LINK_REMINDER, "reminder-1", context, raiser)
            if wants_named:
                sent += _send(db, request, templates.RESERVATION_LINK_PENDING, "reminder-1", context, named)
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
    # Imported here: the parser is a pure helper, but its package pulls in a good deal.
    from app.initiatives.i13.act_requester_directory import parse_requester_recipients

    aliases = parse_requester_recipients(settings.i13_requester_recipients)
    raised = notify_raised(db, now, lookback, aliases)
    linked = notify_new_links(db, now, lookback, aliases)
    reminders = 0
    if settings.assistant_link_reminder_hours > 0:
        reminders = remind_unlinked(
            db, now, timedelta(hours=settings.assistant_link_reminder_hours), lookback, aliases
        )
    return LinkNoticeResult(linked=linked, reminders=reminders, raised=raised)


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
    return f"{result.raised} raised notice(s), {result.linked} linked notice(s), {result.reminders} reminder(s)"

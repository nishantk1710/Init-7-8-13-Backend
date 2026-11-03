"""The one place notifications are created and delivered.

Two steps, kept apart on purpose:

1. :func:`enqueue` -- called by I07, I08 and I13 *inside their own transaction*.
   It writes ``notification`` rows (the outbox) and does not commit, so a
   workflow change and its notifications are saved together or not at all.
   An in-app row is a row the user can see right away, so it is ``SENT``. An
   email row starts ``PENDING``: nothing has been delivered.
2. :func:`dispatch_pending` -- run by the background timer after the commit.
   It sends each pending email through the active adapter and records what
   really happened. A failed email never undoes the workflow step; it is retried
   a limited number of times.

``LOGGED_ONLY`` means the logging adapter ran and nothing was sent, and ``SENT``
is only ever written for a message the provider accepted (or an in-app row).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.integrations.notifications import templates
from app.integrations.notifications.graph_adapter import GraphEmailAdapter, UnconfiguredEmailAdapter
from app.integrations.notifications.links import absolute_url
from app.integrations.notifications.logging_adapter import LoggingNotificationAdapter
from app.integrations.notifications.models import (
    Channel,
    NotificationChannel,
    NotificationEvent,
    NotificationIntent,
    NotificationOutcome,
    NotificationResult,
    Recipient,
    Status,
)
from app.integrations.notifications.port import NotificationPort
from app.integrations.notifications.resolver import INVALID_IDENTITY, is_entra_object_id, resolve_user
from app.models.notification import NotificationRow

logger = logging.getLogger(__name__)

#: How long a claimed email may sit in PROCESSING before another pass may take it
#: (the worker that claimed it died).
CLAIM_LEASE_SECONDS = 600

#: How many idempotency keys go into one ``IN`` list.
_KEY_CHUNK = 500

#: Widths of the ``notification`` columns. A value is checked against them and refused when it
#: does not fit -- never cut short, which would store a different value than the one given.
_LIMITS = {"module": 16, "event_type": 48, "entity_type": 32, "entity_id": 128, "action_path": 500, "plant": 8}
_MAX_KEY = 255
_MAX_EMAIL = 320

_BOTH_CHANNELS = (Channel.IN_APP, Channel.EMAIL)


class NotificationValueError(ValueError):
    """A value does not fit its column. Raised before anything is written."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- Step 1: write the outbox ------------------------------------------------


def _who(recipient: Recipient) -> str:
    return recipient.user_oid or recipient.email or f"unresolved:{recipient.role}:{recipient.plant}"


def _key(event: NotificationEvent, who: str, channel: Channel) -> str:
    """The idempotency key. When the readable form would not fit its 255-character column
    (a long email address, say) it is replaced by its SHA-256, which is still one fixed key
    per message and recipient -- nothing is cut short."""
    readable = f"{event.event_type}|{event.entity_id}|{event.trigger_id}|{who}|{channel.value}"
    return readable if len(readable) <= _MAX_KEY else "h:" + hashlib.sha256(readable.encode("utf-8")).hexdigest()


def _check_event(event: NotificationEvent) -> None:
    for name, limit in _LIMITS.items():
        value = getattr(event, name, None)
        if value is not None and len(value) > limit:
            raise NotificationValueError(f"notification {name} is {len(value)} characters; the column holds {limit}")


def _recipient_problem(recipient: Recipient) -> str | None:
    """Why this recipient cannot be stored as given, or ``None``. Whatever the resolver
    produced, only a valid object id and a sensible address reach the table."""
    if recipient.user_oid and not is_entra_object_id(recipient.user_oid):
        return INVALID_IDENTITY
    if recipient.email and len(recipient.email) > _MAX_EMAIL:
        return f"email address is {len(recipient.email)} characters; the column holds {_MAX_EMAIL}"
    return None


def enqueue(
    db: Session,
    event: NotificationEvent,
    recipients: Iterable[Recipient],
    *,
    channels: Sequence[Channel] = _BOTH_CHANNELS,
    now: datetime | None = None,
    email_enabled: bool | None = None,
) -> list[NotificationRow]:
    """Create the rows for ``event``; return the ones that were new. Does not commit.

    * A recipient that could not be resolved, has an invalid identity, or is a known
      person with no email address becomes a ``FAILED`` email row carrying the reason,
      so the gap is visible. (A known person's in-app notification is still created.)
    * A row whose idempotency key already exists is skipped, which is what makes a
      repeated request or job pass harmless.
    * With email switched off (``NOTIFICATION_EMAIL_ENABLED=false``, or ``email_enabled=False``) no
      email row is created at all; only the in-app inbox rows are. A failure that reaches nobody is
      then recorded as a failed in-app row (it has no user, so no inbox shows it).
    * The rows are written inside a savepoint. If the database refuses them, only
      this notification is rolled back: the caller's own transaction -- an approval,
      say -- is left usable and can still commit. The error is re-raised so the
      caller can log it.

    Raises :class:`NotificationValueError` before writing anything when the event does
    not fit the table.
    """
    _check_event(event)
    email_on = get_settings().notification_email_enabled if email_enabled is None else email_enabled
    if not email_on:
        channels = tuple(c for c in channels if c is not Channel.EMAIL)
    failure_channel = Channel.EMAIL if email_on else Channel.IN_APP
    now = now or _now()
    rendered = templates.render(event.event_type, {"plant": event.plant or "-", **event.context})
    title = (event.title or rendered.title)[:300]
    description = event.description or rendered.body

    # (key, recipient as stored, channel, failure reason or None)
    planned: list[tuple[str, Recipient, Channel, str | None]] = []
    for recipient in recipients:
        problem = _recipient_problem(recipient)
        if problem or recipient.error:
            shown = Recipient(role=recipient.role, plant=recipient.plant)  # nothing unchecked is stored
            planned.append((_key(event, _who(shown), failure_channel), shown, failure_channel, problem or recipient.error))
            continue
        if Channel.IN_APP in channels and recipient.user_oid:
            planned.append((_key(event, _who(recipient), Channel.IN_APP), recipient, Channel.IN_APP, None))
        if Channel.EMAIL in channels:
            if recipient.email:
                planned.append((_key(event, _who(recipient), Channel.EMAIL), recipient, Channel.EMAIL, None))
            elif recipient.email_error:
                planned.append((_key(event, _who(recipient), Channel.EMAIL), recipient, Channel.EMAIL, recipient.email_error))

    if not planned:
        return []
    keys = [key for key, _, _, _ in planned]
    existing: set[str] = set()
    for start in range(0, len(keys), _KEY_CHUNK):  # SQL Server allows 2,100 parameters per statement
        chunk = keys[start : start + _KEY_CHUNK]
        existing.update(db.scalars(select(NotificationRow.idempotency_key).where(NotificationRow.idempotency_key.in_(chunk))))

    created: list[NotificationRow] = []
    for key, recipient, channel, failure in planned:
        if key in existing:
            continue
        existing.add(key)
        if failure:
            status, sent_at, next_attempt, error = Status.FAILED, None, None, failure
        elif channel is Channel.IN_APP:
            status, sent_at, next_attempt, error = Status.SENT, now, None, None
        else:
            status, sent_at, next_attempt, error = Status.PENDING, None, now, None
        created.append(
            NotificationRow(
                idempotency_key=key,
                module=event.module,
                event_type=event.event_type,
                channel=channel.value,
                recipient_user_id=recipient.user_oid,
                recipient_email=recipient.email,
                recipient_role=recipient.role,
                plant=recipient.plant or event.plant,
                title=title,
                description=description,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                action_path=event.action_path,
                status=status.value,
                is_read=False,
                sent_at=sent_at,
                retry_count=0,
                next_attempt_at=next_attempt,
                error=error,
            )
        )

    if not created:
        return []
    try:
        with db.begin_nested():
            db.add_all(created)
            db.flush()
    except Exception as exc:
        # Context for troubleshooting, without the values (a database error text can echo them).
        logger.warning(
            "Notification enqueue failed and was rolled back (module=%s event=%s entity=%s rows=%d error=%s)",
            event.module, event.event_type, event.entity_id, len(created), type(exc).__name__,
        )
        logger.debug("Notification enqueue failure detail", exc_info=True)
        raise
    return created


# --- Step 2: deliver ---------------------------------------------------------


def get_email_adapter(settings: Settings | None = None) -> NotificationPort:
    """The adapter the configuration asks for. The logging adapter unless Graph is chosen."""
    settings = settings or get_settings()
    if settings.notification_email_provider.strip().lower() != "graph":
        return LoggingNotificationAdapter()
    needed = {
        "GRAPH_TENANT_ID": settings.graph_tenant_id,
        "GRAPH_CLIENT_ID": settings.graph_client_id,
        "GRAPH_CLIENT_SECRET": settings.graph_client_secret,
        "GRAPH_SENDER_MAILBOX": settings.graph_sender_mailbox,
    }
    missing = [name for name, value in needed.items() if not value]
    if missing:
        logger.error("NOTIFICATION_EMAIL_PROVIDER=graph but %s is not set; emails will fail until it is", ", ".join(missing))
        return UnconfiguredEmailAdapter(missing)
    return GraphEmailAdapter(
        tenant_id=settings.graph_tenant_id,
        client_id=settings.graph_client_id,
        client_secret=settings.graph_client_secret,
        sender_mailbox=settings.graph_sender_mailbox,
    )


@dataclass(frozen=True)
class DispatchResult:
    sent: int = 0
    logged_only: int = 0
    failed: int = 0


def _intent_for(row: NotificationRow, base_url: str) -> NotificationIntent:
    return NotificationIntent(
        exception_id=row.entity_id or "",
        channel=NotificationChannel.EMAIL,
        recipient=row.recipient_email,
        subject=row.title,
        body=row.description,
        plant=row.plant,
        entity_type=row.entity_type,
        module=row.module,
        action_path=row.action_path,
        action_url=absolute_url(row.action_path, base_url),
        action_label=templates.render(row.event_type).button_label,
    )


def dispatch_pending(
    db: Session,
    adapter: NotificationPort,
    *,
    base_url: str,
    retry_max: int,
    retry_interval_seconds: int,
    now: datetime | None = None,
    batch_size: int = 50,
) -> DispatchResult:
    """Send due emails through ``adapter`` and record the real outcome of each."""
    now = now or _now()
    due = list(
        db.scalars(
            select(NotificationRow)
            .where(
                NotificationRow.channel == Channel.EMAIL.value,
                NotificationRow.recipient_email.is_not(None),
                NotificationRow.status.in_([Status.PENDING.value, Status.FAILED.value, Status.PROCESSING.value]),
                NotificationRow.retry_count < retry_max,
                or_(NotificationRow.next_attempt_at.is_(None), NotificationRow.next_attempt_at <= now),
            )
            .order_by(NotificationRow.created_at, NotificationRow.id)
            .limit(batch_size)
        )
    )
    if not due:
        return DispatchResult()

    # Claim first and commit, so a second worker (or a crash) does not send these twice.
    for row in due:
        row.status = Status.PROCESSING.value
        row.next_attempt_at = now + timedelta(seconds=CLAIM_LEASE_SECONDS)
    db.commit()

    sent = logged = failed = 0
    for row in due:
        try:
            result = adapter.send(_intent_for(row, base_url))
        except Exception as exc:  # noqa: BLE001 -- recorded on the row, and logged
            logger.exception("Notification %s: adapter raised", row.id)
            result = NotificationResult(outcome=NotificationOutcome.FAILED, detail=f"adapter raised: {type(exc).__name__}: {exc}"[:400])

        if result.outcome is NotificationOutcome.SENT:
            row.status, row.sent_at, row.error, row.next_attempt_at = Status.SENT.value, _now(), None, None
            sent += 1
        elif result.outcome is NotificationOutcome.LOGGED_ONLY:
            row.status, row.error, row.next_attempt_at = Status.LOGGED_ONLY.value, None, None
            logged += 1
        else:
            row.retry_count += 1
            row.status = Status.FAILED.value
            row.error = result.detail
            row.next_attempt_at = now + timedelta(seconds=retry_interval_seconds) if row.retry_count < retry_max else None
            failed += 1
            logger.warning("Notification %s failed (attempt %d of %d): %s", row.id, row.retry_count, retry_max, result.detail)
    db.commit()
    return DispatchResult(sent=sent, logged_only=logged, failed=failed)


def dispatch_with_settings(db: Session, settings: Settings | None = None) -> DispatchResult:
    """:func:`dispatch_pending` with the configured adapter, link base and retry limits."""
    settings = settings or get_settings()
    if not settings.notification_email_enabled:
        return DispatchResult()  # email is off: leave any waiting rows untouched, send nothing
    return dispatch_pending(
        db,
        get_email_adapter(settings),
        base_url=settings.notification_frontend_base_url,
        retry_max=settings.notification_retry_max,
        retry_interval_seconds=settings.notification_retry_interval_seconds,
    )


# --- I13's port, backed by the shared tables ---------------------------------


class SharedNotificationPort:
    """I13's ``NotificationPort``, writing to the shared ``notification`` table.

    It only queues. Email goes out later from :func:`dispatch_pending`, so the
    outcome reported here is never ``SENT`` for an email, and an in-app row is
    reported as ``SENT`` only when it really is in someone's inbox.
    """

    def __init__(self, db: Session, identity_aliases: Mapping[str, str] | None = None) -> None:
        self._db = db
        # Lower-cased name -> Entra object id, for a recipient who is known by a name (a SAP requester).
        self._aliases = identity_aliases or {}

    def _identity(self, recipient: str | None) -> str | None:
        """An object id is used as it is; a name that has been mapped to one becomes that id; anything else is
        passed on unchanged, so :func:`resolve_user` refuses it and the failure is recorded."""
        who = (recipient or "").strip()
        if not who or is_entra_object_id(who):
            return recipient
        return self._aliases.get(who.lower(), recipient)

    def send(self, intent: NotificationIntent) -> NotificationResult:
        if intent.channel is NotificationChannel.EMAIL and not get_settings().notification_email_enabled:
            return NotificationResult(outcome=NotificationOutcome.LOGGED_ONLY, detail="email notifications are switched off")
        recipients = resolve_user(
            self._db, self._identity(intent.recipient), plant=intent.plant, fallback_email=get_settings().notification_fallback_email
        )
        recipient = recipients[0]
        if recipient.error:
            enqueue(self._db, self._event(intent), recipients, channels=())
            return NotificationResult(outcome=NotificationOutcome.FAILED, detail=recipient.error)

        in_app = intent.channel is NotificationChannel.PLATFORM_QUEUE
        channel = Channel.IN_APP if in_app else Channel.EMAIL
        created = enqueue(self._db, self._event(intent), recipients, channels=(channel,))
        if in_app:
            if any(row.status == Status.SENT.value for row in created):
                return NotificationResult(outcome=NotificationOutcome.SENT, detail="added to the in-app inbox")
            return NotificationResult(outcome=NotificationOutcome.LOGGED_ONLY, detail="no inbox for this recipient, or already notified")
        if recipient.email_error and not recipient.email:
            return NotificationResult(outcome=NotificationOutcome.FAILED, detail=recipient.email_error)
        if created:
            return NotificationResult(outcome=NotificationOutcome.LOGGED_ONLY, detail="queued for email delivery")
        return NotificationResult(outcome=NotificationOutcome.LOGGED_ONLY, detail="already notified")

    @staticmethod
    def _event(intent: NotificationIntent) -> NotificationEvent:
        return NotificationEvent(
            module=intent.module or "I13",
            event_type=intent.event_type or templates.GENERAL,
            entity_type=intent.entity_type or "EXCEPTION",
            entity_id=intent.exception_id,
            trigger_id=intent.event_type or "general",
            action_path=intent.action_path,
            plant=intent.plant,
            title=intent.subject,
            description=intent.body,
        )


__all__ = [
    "DispatchResult",
    "NotificationValueError",
    "SharedNotificationPort",
    "dispatch_pending",
    "dispatch_with_settings",
    "enqueue",
    "get_email_adapter",
]

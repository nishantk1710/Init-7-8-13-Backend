"""Plain types shared by every notification caller.

Nothing here touches the database or the network. Two groups:

* The delivery types an adapter sees -- ``NotificationIntent`` in,
  ``NotificationResult`` out. They started life in I13's ACT code and keep their
  names and fields so that code is unchanged (``app.initiatives.i13.act.domain``
  re-exports them). The optional fields at the end of the intent are new.
* The stored types -- ``Channel`` and ``Status`` are what the ``notification``
  table holds, and ``Recipient`` is what the resolver hands back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class NotificationChannel(str, Enum):
    """The channel names I13 already uses. ``PLATFORM_QUEUE`` is the in-app inbox."""

    PLATFORM_QUEUE = "PLATFORM_QUEUE"
    EMAIL = "EMAIL"


class NotificationOutcome(str, Enum):
    SENT = "SENT"
    # The intent was captured for audit but nothing was delivered. Never
    # reported as SENT, so "logged" can never be mistaken for "delivered".
    LOGGED_ONLY = "LOGGED_ONLY"
    FAILED = "FAILED"


@dataclass(frozen=True)
class NotificationIntent:
    """One message to one recipient on one channel.

    ``exception_id`` is the id of whatever the message is about -- an
    exception id for I13 and I08, a recommendation id for approvals. The name
    is kept so I13's code did not have to change.
    """

    exception_id: str
    channel: NotificationChannel
    recipient: str | None
    subject: str
    body: str
    # Optional extras. The email adapter uses them; the logging adapter ignores them.
    plant: str | None = None
    entity_type: str | None = None
    module: str | None = None
    # What kind of message this is (``templates.EXCEPTION_ESCALATED``); stored on the row.
    event_type: str | None = None
    # The in-app page this is about (``/approvals``), and the full link built
    # from it for an email. The adapter sends the link; it never builds one.
    action_path: str | None = None
    action_url: str | None = None
    action_label: str | None = None


@dataclass(frozen=True)
class NotificationResult:
    outcome: NotificationOutcome
    detail: str


class Channel(str, Enum):
    """The channel column of the ``notification`` table."""

    EMAIL = "EMAIL"
    IN_APP = "IN_APP"


class Status(str, Enum):
    """The status column of the ``notification`` table.

    ``PENDING`` and ``PROCESSING`` mean nothing has been delivered yet.
    ``LOGGED_ONLY`` is final: the logging adapter ran and nothing was sent.
    """

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SENT = "SENT"
    FAILED = "FAILED"
    LOGGED_ONLY = "LOGGED_ONLY"


@dataclass(frozen=True)
class NotificationEvent:
    """Something that happened and needs telling -- one event, many recipients.

    The wording comes from ``templates`` (by ``event_type`` and ``context``)
    unless ``title``/``description`` are given, which callers with their own
    text (I13) use. ``trigger_id`` identifies this occurrence -- the ledger
    entry that caused it, say -- so a repeat of the same request adds nothing.
    """

    module: str
    event_type: str
    entity_type: str
    entity_id: str
    trigger_id: str
    action_path: str | None = None
    plant: str | None = None
    context: dict[str, str] = field(default_factory=dict)
    title: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class Recipient:
    """Who a notification goes to, as the resolver found them.

    ``error`` is set when nobody could be found; the service records that as a
    failed row instead of dropping the notification silently.

    ``email_error`` is set when the person is known (their in-app notification
    works) but no email address could be found for them. The service records a
    failed email row with that reason, so a missing address is never silent.
    """

    user_oid: str | None = None
    email: str | None = None
    role: str | None = None
    plant: str | None = None
    error: str | None = None
    email_error: str | None = None

"""The one interface every delivery adapter implements."""

from __future__ import annotations

from typing import Protocol

from app.integrations.notifications.models import NotificationIntent, NotificationResult


class NotificationPort(Protocol):
    """Deliver one message.

    An adapter reports what actually happened: ``SENT`` only when the provider
    accepted the message, ``LOGGED_ONLY`` when it was only recorded, ``FAILED``
    otherwise. It should not raise; callers also guard against it if it does.
    """

    def send(self, intent: NotificationIntent) -> NotificationResult: ...

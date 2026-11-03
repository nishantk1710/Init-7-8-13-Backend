"""The adapter used until real email delivery is switched on.

It writes the intent to the log and reports ``LOGGED_ONLY`` -- never ``SENT``,
because nothing was delivered. A reader of the audit trail can therefore never
mistake "we recorded that we meant to notify someone" for "this was delivered".
With no recipient it reports ``FAILED``.

Moved here from I13's ``act_notifications.py`` (which re-exports it) so every
module shares it. The logger name is unchanged.
"""

from __future__ import annotations

import logging

from app.integrations.notifications.models import (
    NotificationIntent,
    NotificationOutcome,
    NotificationResult,
)

logger = logging.getLogger("app.initiatives.i13.act.notifications")


class LoggingNotificationAdapter:
    """Logs the intent. Never raises."""

    def send(self, intent: NotificationIntent) -> NotificationResult:
        if not intent.recipient:
            detail = f"no recipient resolved for {intent.channel.value} notification"
            logger.warning(
                "Notification not sent (%s): entity=%s subject=%s",
                detail,
                intent.exception_id,
                intent.subject,
            )
            return NotificationResult(outcome=NotificationOutcome.FAILED, detail=detail)

        logger.info(
            "Notification (logging adapter, not delivered): channel=%s recipient=%s entity=%s subject=%s",
            intent.channel.value,
            intent.recipient,
            intent.exception_id,
            intent.subject,
        )
        return NotificationResult(
            outcome=NotificationOutcome.LOGGED_ONLY,
            detail=f"logged only -- no {intent.channel.value.lower()} provider configured yet",
        )


class NullNotificationAdapter:
    """Discards every notification. For tests that want no delivery attempt at all."""

    def send(self, intent: NotificationIntent) -> NotificationResult:
        return NotificationResult(outcome=NotificationOutcome.LOGGED_ONLY, detail="discarded by NullNotificationAdapter")

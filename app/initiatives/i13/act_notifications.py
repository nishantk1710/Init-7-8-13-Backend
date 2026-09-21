"""W6.6 ``NotificationPort`` adapters.

TODAY: no real email service is configured anywhere in this codebase
(``app/integrations/notifications/__init__.py`` is an unimplemented stub --
see that module). ``LoggingNotificationAdapter`` is what every W6.6 route
wires in: it records notification intent/outcome for audit (via
``ExceptionRepository.record_notification``, called by
``app.initiatives.i13.act.service``) and logs at INFO level, but always
reports ``LOGGED_ONLY`` -- never ``SENT`` -- because nothing was actually
delivered. A reader of the audit trail or the notification table can
therefore never mistake "we recorded that we meant to notify someone" for
"this notification was delivered".

LATER: a production adapter (SMTP, Microsoft Graph, Azure Communication
Services) implements the same ``NotificationPort.send`` method and is wired
in instead -- ``app.initiatives.i13.act.service`` does not change, and does
not import any of those SDKs.
"""

from __future__ import annotations

import logging

from app.initiatives.i13.act.domain import NotificationIntent, NotificationOutcome, NotificationResult

logger = logging.getLogger("app.initiatives.i13.act.notifications")


class LoggingNotificationAdapter:
    """TODAY's ``NotificationPort``: logs the intent, records it as
    ``LOGGED_ONLY`` (channel had a recipient) or ``FAILED`` (no recipient to
    notify -- e.g. routing never resolved anyone). Never raises: a
    logging/formatting failure inside this adapter must not be able to
    corrupt the caller's exception-state transaction (see
    ``app.initiatives.i13.act.service._notify``, which also guards this at
    the call site)."""

    def send(self, intent: NotificationIntent) -> NotificationResult:
        if not intent.recipient:
            detail = f"no recipient resolved for {intent.channel.value} notification"
            logger.warning("ACT notification not sent (%s): exception=%s subject=%s", detail, intent.exception_id, intent.subject)
            return NotificationResult(outcome=NotificationOutcome.FAILED, detail=detail)

        logger.info(
            "ACT notification (dev/logging adapter, not delivered): channel=%s recipient=%s exception=%s subject=%s",
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
    """Discards every notification -- used only where a test explicitly
    wants delivery attempts to not happen at all (most tests should use a
    fake/spy instead, so they can assert on what was sent)."""

    def send(self, intent: NotificationIntent) -> NotificationResult:
        return NotificationResult(outcome=NotificationOutcome.LOGGED_ONLY, detail="discarded by NullNotificationAdapter")

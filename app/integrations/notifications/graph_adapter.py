"""Email through Microsoft Graph (``POST /users/{mailbox}/sendMail``).

Uses the ``Mail.Send`` *application* permission and one shared mailbox as the
sender, with the ``azure-identity`` and ``requests`` packages the project already
has. Nothing here is switched on by default: it is used only when
``NOTIFICATION_EMAIL_PROVIDER=graph`` and the four ``GRAPH_*`` settings are
filled in, which needs the tenant admin's consent first.

It reports what happened: ``SENT`` only when Graph answers ``202 Accepted``;
every other result is ``FAILED`` with the reason. It never raises.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable

import requests
from azure.identity import ClientSecretCredential

from app.integrations.notifications.models import NotificationIntent, NotificationOutcome, NotificationResult

logger = logging.getLogger(__name__)

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
SEND_MAIL_URL = "https://graph.microsoft.com/v1.0/users/{mailbox}/sendMail"
TIMEOUT_SECONDS = 30


def _failed(detail: str) -> NotificationResult:
    return NotificationResult(outcome=NotificationOutcome.FAILED, detail=detail[:400])


def build_html(intent: NotificationIntent) -> str:
    """The message body: the text, then a button linking to the page."""
    text = html.escape(intent.body).replace("\n", "<br>")
    label = html.escape(intent.action_label or "Open")
    url = html.escape(intent.action_url or "", quote=True)
    return (
        f"<p>{text}</p>"
        f'<p><a href="{url}" style="background:#0b5cad;color:#ffffff;padding:10px 18px;'
        f'text-decoration:none;border-radius:4px;display:inline-block">{label}</a></p>'
    )


class GraphEmailAdapter:
    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        sender_mailbox: str,
        post: Callable[..., requests.Response] | None = None,
        token_provider: Callable[[], str] | None = None,
    ) -> None:
        self._mailbox = sender_mailbox
        self._post = post or requests.post
        if token_provider is not None:
            self._token = token_provider
        else:
            credential = ClientSecretCredential(tenant_id, client_id, client_secret)
            self._token = lambda: credential.get_token(GRAPH_SCOPE).token

    def send(self, intent: NotificationIntent) -> NotificationResult:
        if not intent.recipient:
            return _failed("no recipient resolved for EMAIL notification")
        if not intent.action_url:
            # Every email must link to the page it is about, so no link means no send.
            return _failed("no link for the email: set NOTIFICATION_FRONTEND_BASE_URL")
        payload = {
            "message": {
                "subject": intent.subject,
                "body": {"contentType": "HTML", "content": build_html(intent)},
                "toRecipients": [{"emailAddress": {"address": intent.recipient}}],
            },
            "saveToSentItems": False,
        }
        try:
            response = self._post(
                SEND_MAIL_URL.format(mailbox=self._mailbox),
                json=payload,
                headers={"Authorization": f"Bearer {self._token()}"},
                timeout=TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 -- reported as a failed attempt, and logged
            logger.exception("Graph sendMail raised for entity=%s", intent.exception_id)
            return _failed(f"Graph request failed: {type(exc).__name__}: {exc}")
        if response.status_code == 202:
            return NotificationResult(outcome=NotificationOutcome.SENT, detail="accepted by Microsoft Graph")
        logger.warning("Graph sendMail refused (HTTP %s) for entity=%s", response.status_code, intent.exception_id)
        return _failed(f"Graph answered HTTP {response.status_code}: {response.text[:200]}")


class UnconfiguredEmailAdapter:
    """Used when Graph is chosen but its settings are incomplete: every email fails, loudly."""

    def __init__(self, missing: list[str]) -> None:
        self._detail = "Graph email is not configured; missing " + ", ".join(missing)

    def send(self, intent: NotificationIntent) -> NotificationResult:
        return _failed(self._detail)

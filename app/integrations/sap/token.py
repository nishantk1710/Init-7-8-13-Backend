"""OAuth client-credentials token for CPI.

Lifted from ``data-generator/cpi_discovery.py``, which has been exchanging this
token against live CPI for weeks. What changed in the move:

* it is a class, not a module-level variable, so two callers cannot fight over
  one token;
* a missing setting raises ``AuthError`` instead of calling ``sys.exit`` -- the
  script could afford to kill the process, a web worker cannot;
* the transport is injected, so the tests need no network.

Expiry is tracked rather than assumed. The script re-fetched on a 401 and that
was enough for a batch run; a long-lived process should not deliberately make a
call it knows will fail.
"""

from __future__ import annotations

import time
from typing import Protocol

import requests

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.integrations.sap.errors import AuthError

logger = get_logger(__name__)

# Refresh this many seconds before the token actually expires, so a call never
# starts with a token that dies mid-flight.
_EXPIRY_MARGIN_SECONDS = 60

# Used when SAP does not tell us. Short enough to be safe, long enough not to
# re-authenticate constantly.
_DEFAULT_LIFETIME_SECONDS = 3600


class TokenTransport(Protocol):
    """The one call the token provider makes. Injected so tests need no network."""

    def __call__(
        self,
        url: str,
        *,
        data: dict,
        auth: tuple[str, str],
        timeout: int,
        verify: str | bool,
    ) -> requests.Response: ...


class TokenProvider:
    """Fetches and caches a bearer token."""

    def __init__(
        self,
        settings: Settings | None = None,
        transport: TokenTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._transport = transport or requests.post
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get(self, *, force_refresh: bool = False) -> str:
        """A valid bearer token, fetching one only when needed."""
        if not force_refresh and self._token and time.time() < self._expires_at:
            return self._token
        return self._fetch()

    def invalidate(self) -> None:
        """Forget the cached token. Called after a 401 -- see transport.py."""
        self._token = None
        self._expires_at = 0.0

    def _fetch(self) -> str:
        settings = self._settings
        missing = [
            name
            for name, value in (
                ("CPI_TOKEN_URL", settings.cpi_token_url),
                ("CPI_CLIENT_ID", settings.cpi_client_id),
                ("CPI_CLIENT_SECRET", settings.cpi_client_secret),
            )
            if not value
        ]
        if missing:
            raise AuthError(
                f"Cannot request a CPI token: {', '.join(missing)} not set. "
                "See .env.example, 'SAP / CPI'."
            )

        try:
            response = self._transport(
                settings.cpi_token_url,
                data={"grant_type": "client_credentials"},
                auth=(settings.cpi_client_id, settings.cpi_client_secret),
                timeout=settings.cpi_timeout_seconds,
                verify=settings.cpi_ca_bundle or True,
            )
        except requests.RequestException as exc:
            # A network failure fetching a token is still an auth failure from
            # the caller's point of view: no token, no call.
            raise AuthError(f"CPI token request failed: {exc}") from exc

        if not response.ok:
            raise AuthError(
                "CPI token request rejected",
                status=response.status_code,
                body=response.text,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AuthError("CPI token response was not JSON", body=response.text) from exc

        token = payload.get("access_token")
        if not token:
            raise AuthError("CPI token response carried no access_token", body=response.text)

        lifetime = int(payload.get("expires_in") or _DEFAULT_LIFETIME_SECONDS)
        self._token = token
        self._expires_at = time.time() + max(lifetime - _EXPIRY_MARGIN_SECONDS, 0)
        logger.info("CPI token acquired, valid for %ss", lifetime)
        return token

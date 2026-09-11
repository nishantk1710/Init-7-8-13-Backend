"""The single HTTP door to SAP.

Every SAP call in this application goes through ``CpiTransport.get``. Nothing
above it builds a URL, holds a token, or reads a status code.

The shape is not ours to choose. SAP is reached through one CPI iFlow that takes
the real OData request as two query parameters::

    GET <cpi_base_url><cpi_path>?APIPath=sap/opu/odata/sap/<SERVICE>/<Set>
                                &APIQuery=$filter=...&$top=...

so there are no per-service URLs to configure, and callers never see this.

Retry behaviour is lifted from ``cpi_discovery.py``, which has run it against
live CPI for weeks:

* **401 -> refresh the token once and retry.** Tokens expire mid-run; this is
  the single most valuable line in the original script.
* **5xx -> exponential backoff**, up to ``max_retries``.
* **Everything else -> classify and raise.** No retry on a 404 or a bad filter;
  retrying cannot fix either.

The ``Accept`` header negotiates JSON but tolerates XML. That is not cosmetic:
``$metadata`` is XML only, and asking for JSON alone earns an HTTP 406.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

import requests

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.integrations.sap.errors import SapError, TransientError, classify
from app.integrations.sap.token import TokenProvider

logger = get_logger(__name__)

# JSON preferred, XML accepted -- $metadata is XML only (the HTTP 406 lesson).
_ACCEPT = "application/json, application/xml;q=0.9, */*;q=0.8"

_DEFAULT_MAX_RETRIES = 3


class HttpTransport(Protocol):
    """The one call this module makes. Injected so tests need no network."""

    def __call__(
        self,
        url: str,
        *,
        params: dict,
        headers: dict,
        timeout: int,
        verify: str | bool,
    ) -> requests.Response: ...


class CpiTransport:
    """Authenticated HTTP against the CPI generic consumption endpoint."""

    def __init__(
        self,
        settings: Settings | None = None,
        tokens: TokenProvider | None = None,
        transport: HttpTransport | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        sleep: Any = time.sleep,
    ) -> None:
        self._settings = settings or get_settings()
        self._tokens = tokens or TokenProvider(self._settings)
        self._transport = transport or requests.get
        self._max_retries = max_retries
        # Injected so tests do not actually wait out the backoff.
        self._sleep = sleep

    @property
    def endpoint(self) -> str:
        """The one URL every call goes to."""
        base = self._settings.cpi_base_url.rstrip("/")
        if not base:
            raise SapError(
                "CPI_BASE_URL is not set. See .env.example, 'SAP / CPI'."
            )
        return base + self._settings.cpi_path

    def get(self, api_path: str, api_query: str = "", *, context: str | None = None) -> str:
        """Perform one CPI call and return the raw response body.

        ``api_path`` is the OData path, e.g.
        ``sap/opu/odata/sap/ZVZI_KPI02_SHARED_SRV/MaterialPlantSet``.
        ``api_query`` is the OData query without a leading ``?``.

        Returns text, not a parsed object: the caller knows whether it asked for
        JSON rows or XML metadata, and this layer does not need to.
        """
        label = context or api_path
        last_error: SapError | None = None

        for attempt in range(self._max_retries):
            token = self._tokens.get()
            started = time.time()

            try:
                response = self._transport(
                    self.endpoint,
                    params={"APIPath": api_path, "APIQuery": api_query},
                    headers={"Authorization": f"Bearer {token}", "Accept": _ACCEPT},
                    timeout=self._settings.cpi_timeout_seconds,
                    verify=self._settings.cpi_ca_bundle or True,
                )
            except requests.RequestException as exc:
                # A connection reset or read timeout is transient by nature.
                last_error = TransientError(f"{label}: {type(exc).__name__}: {exc}")
                if attempt < self._max_retries - 1:
                    self._sleep(2**attempt)
                    continue
                raise last_error from exc

            elapsed = time.time() - started
            logger.debug(
                "CPI %s -> %s in %.1fs (%d bytes)",
                api_path,
                response.status_code,
                elapsed,
                len(response.content or b""),
            )

            if response.ok:
                return response.text

            # A token can expire between two calls of a long extraction. Refresh
            # once and retry; a second 401 is a real credentials problem.
            if response.status_code == 401 and attempt == 0:
                logger.info("CPI returned 401; refreshing the token and retrying once")
                self._tokens.invalidate()
                continue

            error = classify(response.status_code, response.text, label)

            if isinstance(error, TransientError) and attempt < self._max_retries - 1:
                delay = 2**attempt
                logger.warning(
                    "CPI %s returned %s; retrying in %ss", label, response.status_code, delay
                )
                self._sleep(delay)
                last_error = error
                continue

            raise error

        raise last_error or TransientError(f"{label}: exhausted {self._max_retries} attempts")

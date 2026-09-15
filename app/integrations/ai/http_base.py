"""Shared HTTP behaviour for provider adapters: retry, timeout, token logging.

W1.5 asks for retry, timeout and token logging. None of that is provider-
specific, so it lives here once and each adapter supplies only the three things
that genuinely differ: the URL, the headers, and how to read the response.

That split is what makes the abstraction real rather than decorative. Foundry
and an OpenAI-compatible endpoint disagree about all three -- different URL
shape, different auth header, different response envelope -- and both are about
forty lines on top of this.

Retry policy mirrors the SAP transport, which has been proven against a live
service for weeks: 5xx, 408 and 429 are transient and retried with exponential
backoff; 401 and 403 are not, because retrying with the same credentials cannot
work; every other 4xx is the request's fault and is not retried either.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from typing import Any, Protocol, Sequence

import requests

from app.core.ai import (
    AINotConfiguredError,
    AIProviderError,
    AITimeoutError,
    AITransientError,
    Completion,
    LLMProvider,
    Message,
    TokenUsage,
)
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class HttpTransport(Protocol):
    """The one call an adapter makes. Injected so tests need no network."""

    def __call__(
        self, url: str, *, json: dict, headers: dict, timeout: int
    ) -> requests.Response: ...


class HttpLLMProvider(LLMProvider):
    """Base for any provider reached over HTTP."""

    def __init__(
        self,
        settings: Settings | None = None,
        transport: HttpTransport | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self._settings = settings or get_settings()
        self._transport = transport or requests.post
        # Injected so tests do not wait out the backoff.
        self._sleep = sleep

    # --- What each provider must supply -----------------------------------

    @abstractmethod
    def _endpoint(self, model: str) -> str:
        """The full URL for a completion against ``model``."""

    @abstractmethod
    def _headers(self) -> dict[str, str]:
        """Auth and content headers."""

    @abstractmethod
    def _body(
        self,
        messages: Sequence[Message],
        model: str,
        max_tokens: int,
        temperature: float | None,
    ) -> dict:
        """The request body in this provider's shape."""

    @abstractmethod
    def _parse(self, payload: dict, model: str) -> tuple[str, TokenUsage, str | None]:
        """Read text, usage and finish reason out of this provider's response."""

    @abstractmethod
    def _default_model(self) -> str:
        """The configured model or deployment name."""

    def _require(self, **values: str) -> None:
        """Raise a single clear error naming every missing setting."""
        missing = [name.upper() for name, value in values.items() if not value]
        if missing:
            raise AINotConfiguredError(
                f"{self.name}: {', '.join(missing)} not set. See .env.example, "
                "'AI service layer'."
            )

    # --- The shared path ---------------------------------------------------

    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        model: str | None = None,
    ) -> Completion:
        chosen = model or self._default_model()
        limit = max_tokens or self._settings.llm_max_tokens
        body = self._body(messages, chosen, limit, temperature)
        url = self._endpoint(chosen)

        started = time.monotonic()
        payload = self._request(url, body)
        elapsed_ms = int((time.monotonic() - started) * 1000)

        text, usage, finish_reason = self._parse(payload, chosen)

        # Token logging, as W1.5 requires. At INFO because spend is operational
        # information someone will want without turning on debug logging.
        logger.info(
            "%s completion: model=%s in=%d out=%d total=%d %dms finish=%s",
            self.name,
            chosen,
            usage.input_tokens,
            usage.output_tokens,
            usage.total,
            elapsed_ms,
            finish_reason,
        )

        return Completion(
            text=text,
            model=chosen,
            provider=self.name,
            usage=usage,
            finish_reason=finish_reason,
            latency_ms=elapsed_ms,
        )

    def _request(self, url: str, body: dict) -> dict:
        """POST with retry. Returns the decoded payload or raises."""
        last: AIProviderError | None = None

        for attempt in range(self._settings.llm_max_retries):
            try:
                response = self._transport(
                    url,
                    json=body,
                    headers=self._headers(),
                    timeout=self._settings.llm_timeout_seconds,
                )
            except requests.Timeout as exc:
                last = AITimeoutError(
                    f"{self.name}: no response within "
                    f"{self._settings.llm_timeout_seconds}s"
                )
                if attempt < self._settings.llm_max_retries - 1:
                    self._sleep(2**attempt)
                    continue
                raise last from exc
            except requests.RequestException as exc:
                last = AITransientError(f"{self.name}: {type(exc).__name__}: {exc}")
                if attempt < self._settings.llm_max_retries - 1:
                    self._sleep(2**attempt)
                    continue
                raise last from exc

            if response.ok:
                try:
                    return response.json()
                except ValueError as exc:
                    raise AIProviderError(
                        f"{self.name}: response was not JSON",
                        status=response.status_code,
                        body=response.text,
                    ) from exc

            if response.status_code in (401, 403):
                # Retrying with the same credentials cannot help.
                raise AINotConfiguredError(
                    f"{self.name}: authentication rejected (HTTP {response.status_code}). "
                    "Check the key and the endpoint."
                )

            if response.status_code in _RETRYABLE_STATUS:
                last = AITransientError(
                    f"{self.name}: provider error",
                    status=response.status_code,
                    body=response.text,
                )
                if attempt < self._settings.llm_max_retries - 1:
                    delay = 2**attempt
                    logger.warning(
                        "%s returned %s; retrying in %ss",
                        self.name,
                        response.status_code,
                        delay,
                    )
                    self._sleep(delay)
                    continue
                raise last

            raise AIProviderError(
                f"{self.name}: request rejected",
                status=response.status_code,
                body=response.text,
            )

        raise last or AITransientError(f"{self.name}: retries exhausted")

    def check_connection(self) -> None:
        """A minimal completion, to prove the endpoint and key work."""
        self.complete([Message("user", "ping")], max_tokens=8)

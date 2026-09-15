"""The AI service layer: ports, errors, and the provider factory.

Four features need a language model -- I07's recommendation rationale, I08's
free-text screening for repair language, I13's quantity-suggestion reason, and
the reservation assistant. If each called a provider directly, changing provider
would mean changing four places and finding the fourth in production.

So this is a socket. Business logic asks for a *completion*; it never names a
provider, an endpoint, a deployment or a model. One module knows which provider
is plugged in, and that module is chosen from configuration:

    LLM_PROVIDER=stub       deterministic, no network -- the default
    LLM_PROVIDER=foundry    Microsoft Foundry
    LLM_PROVIDER=openai     any OpenAI-compatible endpoint

The pattern is the same one already used three times here -- ``DATABASE_URL``
picks a dialect, ``STORAGE_URL`` picks a storage adapter, and the SAP contract
picks a service. Lazy construction, injected transport, configuration as the
only thing that differs between environments.

**Two ports, not one.** ``LLMProvider`` covers language work. ``Forecaster``
covers demand prediction, and is deliberately thin: Croston, SBA and the ML
challenger are in-process statistics with no external provider, so W4.2 does not
depend on this module at all. The port exists so that *if* forecasting later
moves to a hosted endpoint, the call sites do not change. Building it out
further today would be speculative.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Sequence

from app.core.config import Settings, get_settings


# --- Errors ---------------------------------------------------------------


class AIError(RuntimeError):
    """Base for every AI service failure."""


class AINotConfiguredError(AIError):
    """No provider is configured, or its settings are incomplete."""


class AIProviderError(AIError):
    """The provider was reached and refused, or answered unusably."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        # Truncated: provider error bodies can be long, and this reaches logs.
        self.body = (body or "")[:2000] or None


class AITransientError(AIProviderError):
    """A 5xx, a timeout or a rate limit. Worth retrying with backoff."""


class AITimeoutError(AITransientError):
    """The provider did not answer inside the configured timeout."""


# --- What a completion is -------------------------------------------------


@dataclass(frozen=True)
class Message:
    """One turn. ``role`` is ``system``, ``user`` or ``assistant``.

    Deliberately the lowest common denominator across providers: every chat API
    in use understands a role and text. Anything provider-specific belongs in
    the adapter, not here.
    """

    role: str
    content: str


@dataclass(frozen=True)
class TokenUsage:
    """What a call cost. Zeros mean the provider did not report usage."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class Completion:
    """What a provider returned, plus what it took to get it."""

    text: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    provider: str = ""
    prompt_id: str | None = None
    """Which registry prompt produced this, when one was used.

    Carried so an AI-written rationale can be traced back to the exact prompt
    version behind it -- the programme is human-gated and audited, and "the model
    said so" is not an acceptable provenance.
    """
    prompt_version: int | None = None
    finish_reason: str | None = None
    latency_ms: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# --- The LLM port ---------------------------------------------------------


class LLMProvider(ABC):
    """A language model, whoever supplies it.

    Small on purpose. Every method here has to be implemented, and proven by the
    shared conformance suite, once per provider.
    """

    name: str = "unknown"

    @abstractmethod
    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        model: str | None = None,
    ) -> Completion:
        """One request, one answer.

        ``model`` overrides the configured default; callers normally omit it and
        let the registry decide.
        """

    @abstractmethod
    def check_connection(self) -> None:
        """Prove the provider is reachable and configured. Raises on failure.

        Used by the readiness endpoint, the same way the database and storage
        ports are checked.
        """


# --- The forecast port ----------------------------------------------------


@dataclass(frozen=True)
class ForecastPoint:
    """One period of predicted demand."""

    period: str
    quantity: float


@dataclass(frozen=True)
class Forecast:
    """A demand forecast and enough provenance to defend it."""

    points: list[ForecastPoint]
    method: str
    """Which technique produced this -- "croston", "sba", an ML model name."""
    horizon: int = 0
    detail: dict[str, Any] = field(default_factory=dict)


class Forecaster(ABC):
    """Demand forecasting, wherever it runs.

    Thin by design. I07's engine (W4.2) is in-process statistics -- Croston, SBA
    and an ML challenger selected by backtest -- with no external provider, and
    it does not depend on this module. This port exists so the call sites survive
    forecasting later moving to a hosted endpoint. Do not grow it speculatively.
    """

    name: str = "unknown"

    @abstractmethod
    def forecast(self, history: Sequence[float], *, horizon: int = 1) -> Forecast:
        """Predict ``horizon`` periods from a consumption series."""

    @abstractmethod
    def check_connection(self) -> None:
        """Prove the forecaster is usable. In-process implementations pass trivially."""


# --- Factory --------------------------------------------------------------

PROVIDERS = ("stub", "foundry", "openai")


def build_provider(settings: Settings | None = None, transport: Any = None) -> LLMProvider:
    """Construct the configured provider. The only place that mapping lives.

    ``transport`` is passed through to HTTP-backed adapters so tests need no
    network -- the same seam the SAP client uses.
    """
    settings = settings or get_settings()
    choice = (settings.llm_provider or "stub").strip().lower()

    if choice == "stub":
        from app.integrations.ai.stub import StubProvider

        return StubProvider()

    if choice == "foundry":
        from app.integrations.ai.foundry import FoundryProvider

        return FoundryProvider(settings, transport=transport)

    if choice == "openai":
        from app.integrations.ai.openai_compatible import OpenAICompatibleProvider

        return OpenAICompatibleProvider(settings, transport=transport)

    raise AINotConfiguredError(
        f"Unknown LLM_PROVIDER {choice!r}. Supported: {', '.join(PROVIDERS)}."
    )


@lru_cache
def get_llm() -> LLMProvider:
    """The process-wide provider, built on first use.

    Lazy for the same reason the database engine and storage adapter are:
    importing the application must not require a provider to be configured, or
    the liveness endpoint and the test suite stop working on a bare machine.
    """
    return build_provider()


@lru_cache
def get_forecaster() -> Forecaster:
    """The process-wide forecaster. In-process today; see ``Forecaster``."""
    from app.integrations.ai.local_forecast import CrostonForecaster

    return CrostonForecaster()


def reset_ai_cache() -> None:
    """Forget the cached provider and forecaster. For tests that change config."""
    get_llm.cache_clear()
    get_forecaster.cache_clear()

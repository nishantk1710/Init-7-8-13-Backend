"""Microsoft Foundry adapter. VZI's provider.

Foundry exposes **two different request shapes**, and which one an endpoint
speaks is not a detail you can shrug at -- the wrong one is a 404, not a
graceful degradation.

**v1 (the newer AI Foundry surface).** Endpoints on ``services.ai.azure.com``
ending ``/openai/v1``. OpenAI-compatible::

    POST {endpoint}/chat/completions
    Authorization: Bearer <key>
    {"model": "gpt-4o", "messages": [...], "max_tokens": N}

**deployments (the classic Azure OpenAI surface).** Endpoints on
``openai.azure.com``::

    POST {endpoint}/openai/deployments/{deployment}/chat/completions?api-version=...
    api-key: <key>
    {"messages": [...], "max_tokens": N}

Three things differ: where the model is named (body vs URL), the auth header,
and whether an ``api-version`` is required. VZI's endpoint is

    https://oai-vzi-aicom-nonprod-san.services.ai.azure.com/openai/v1

which is v1. Building the classic path onto it would give
``.../openai/v1/openai/deployments/gpt-4o/...`` -- a doubled ``/openai/`` and a
confusing 404 that looks like a permissions problem.

``FOUNDRY_API_STYLE`` defaults to ``auto`` and infers the shape from the
endpoint. Both are implemented and both are tested, so an endpoint change is
configuration rather than code.

The response envelope is identical between the two, so parsing is shared.
"""

from __future__ import annotations

from typing import Sequence

from app.core.ai import AIProviderError, Message, TokenUsage
from app.integrations.ai.http_base import HttpLLMProvider

V1 = "v1"
DEPLOYMENTS = "deployments"

# The marker Foundry uses for its OpenAI-compatible surface.
_V1_SUFFIX = "/openai/v1"


class FoundryProvider(HttpLLMProvider):
    name = "foundry"

    # --- Which shape are we talking? --------------------------------------

    @property
    def api_style(self) -> str:
        """``v1`` or ``deployments``, from configuration or inferred."""
        configured = (self._settings.foundry_api_style or "auto").strip().lower()
        if configured in (V1, DEPLOYMENTS):
            return configured
        if configured != "auto":
            raise AIProviderError(
                f"Unknown FOUNDRY_API_STYLE {configured!r}. "
                f"Use {V1!r}, {DEPLOYMENTS!r} or 'auto'."
            )
        endpoint = (self._settings.foundry_endpoint or "").rstrip("/")
        return V1 if endpoint.endswith(_V1_SUFFIX) else DEPLOYMENTS

    def _default_model(self) -> str:
        return self._settings.foundry_deployment

    # --- The three things that differ -------------------------------------

    def _endpoint(self, model: str) -> str:
        self._require(
            foundry_endpoint=self._settings.foundry_endpoint,
            foundry_api_key=self._settings.foundry_api_key,
            foundry_deployment=model,
        )
        base = self._settings.foundry_endpoint.rstrip("/")

        if self.api_style == V1:
            # The endpoint already carries /openai/v1; appending anything else
            # is what produces the doubled path.
            return f"{base}/chat/completions"

        return (
            f"{base}/openai/deployments/{model}/chat/completions"
            f"?api-version={self._settings.foundry_api_version}"
        )

    def _headers(self) -> dict[str, str]:
        key = self._settings.foundry_api_key
        headers = {"Content-Type": "application/json"}

        if self.api_style == V1:
            # The OpenAI-compatible surface authenticates with a bearer token.
            headers["Authorization"] = f"Bearer {key}"
            # Azure also accepts api-key here and ignores headers it does not
            # use. Sending both costs nothing and removes a whole class of
            # first-run 401 that looks like a bad key rather than a bad header.
            headers["api-key"] = key
        else:
            headers["api-key"] = key

        return headers

    def _body(
        self,
        messages: Sequence[Message],
        model: str,
        max_tokens: int,
        temperature: float | None,
    ) -> dict:
        body: dict = {
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
        }
        if self.api_style == V1:
            # v1 names the model in the body; the classic shape puts it in the URL.
            body["model"] = model
        if temperature is not None:
            body["temperature"] = temperature
        return body

    # --- Shared between both shapes ---------------------------------------

    def _parse(self, payload: dict, model: str) -> tuple[str, TokenUsage, str | None]:
        choices = payload.get("choices") or []
        if not choices:
            error = payload.get("error")
            if error:
                # Azure returns a structured error inside a 200 in some cases.
                raise AIProviderError(
                    f"{self.name}: {error.get('message') or error}",
                    body=str(error),
                )
            raise AIProviderError(f"{self.name}: response carried no choices")

        first = choices[0]
        text = (first.get("message") or {}).get("content") or ""
        usage = payload.get("usage") or {}
        return (
            text,
            TokenUsage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            ),
            first.get("finish_reason"),
        )

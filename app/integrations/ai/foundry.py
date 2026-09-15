"""Microsoft Foundry adapter.

VZI's designated provider. Foundry serves chat models on a deployment-scoped
URL with an ``api-key`` header and an API version as a query parameter::

    POST {endpoint}/openai/deployments/{deployment}/chat/completions?api-version=...
    api-key: <key>
    {"messages": [...], "max_tokens": N}
    -> {"choices": [{"message": {"content": "..."}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": N, "completion_tokens": N}}

ONE OPEN QUESTION, and it decides whether this file is right.
--------------------------------------------------------------
Foundry hosts several model families, and the wire shape above is the
chat-completions one. If VZI's deployment turns out to be a Claude model, the
correct client is the official Anthropic SDK's Foundry client rather than raw
HTTP, and the response shape is ``content[0].text`` rather than
``choices[0].message.content``.

W1.1's Day-0 checklist lists "Foundry resource and model deployments" as an open
item with VZI IT. Until that is answered this adapter is built for the
chat-completions shape, which is the documented Foundry inference surface and
covers the Azure OpenAI family. Swapping it is one file and its tests -- the
port above does not change, which is the entire point of having one.

Endpoint and key are the only things that wait for Azure. Everything here is
tested against an injected transport.
"""

from __future__ import annotations

from typing import Sequence

from app.core.ai import Message, TokenUsage
from app.integrations.ai.http_base import HttpLLMProvider


class FoundryProvider(HttpLLMProvider):
    name = "foundry"

    def _default_model(self) -> str:
        return self._settings.foundry_deployment

    def _endpoint(self, model: str) -> str:
        self._require(
            foundry_endpoint=self._settings.foundry_endpoint,
            foundry_api_key=self._settings.foundry_api_key,
            foundry_deployment=model,
        )
        base = self._settings.foundry_endpoint.rstrip("/")
        return (
            f"{base}/openai/deployments/{model}/chat/completions"
            f"?api-version={self._settings.foundry_api_version}"
        )

    def _headers(self) -> dict[str, str]:
        # Foundry authenticates with `api-key`, not a Bearer token -- one of the
        # concrete ways it differs from the OpenAI-compatible adapter.
        return {
            "api-key": self._settings.foundry_api_key,
            "Content-Type": "application/json",
        }

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
        if temperature is not None:
            body["temperature"] = temperature
        return body

    def _parse(self, payload: dict, model: str) -> tuple[str, TokenUsage, str | None]:
        choices = payload.get("choices") or []
        if not choices:
            from app.core.ai import AIProviderError

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

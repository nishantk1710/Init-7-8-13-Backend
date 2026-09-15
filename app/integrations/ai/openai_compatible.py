"""An OpenAI-compatible endpoint. The alternate provider.

W1.5 asks for "Foundry adapter plus one alternate", and the alternate is not
decoration: **an abstraction with a single implementation is untested.** You only
find out you have baked provider-specific assumptions into a port when you try
to plug something else into it.

This one is chosen because it differs from Foundry in every place that matters:

    Foundry     {endpoint}/openai/deployments/{deployment}/chat/completions?api-version=...
                api-key: <key>
    This        {base}/chat/completions
                Authorization: Bearer <key>          + "model" in the body

Different URL shape, different auth header, and the model named in the body
rather than the path. If the port survives both, it is a port. It also covers
any OpenAI-compatible gateway, which is the most widely-implemented shape.

The response envelope happens to match Foundry's, so parsing is inherited.
"""

from __future__ import annotations

from typing import Sequence

from app.core.ai import Message, TokenUsage
from app.integrations.ai.foundry import FoundryProvider
from app.integrations.ai.http_base import HttpLLMProvider


class OpenAICompatibleProvider(HttpLLMProvider):
    name = "openai"

    def _default_model(self) -> str:
        return self._settings.llm_model

    def _endpoint(self, model: str) -> str:
        self._require(
            llm_base_url=self._settings.llm_base_url,
            llm_api_key=self._settings.llm_api_key,
            llm_model=model,
        )
        return f"{self._settings.llm_base_url.rstrip('/')}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.llm_api_key}",
            "Content-Type": "application/json",
        }

    def _body(
        self,
        messages: Sequence[Message],
        model: str,
        max_tokens: int,
        temperature: float | None,
    ) -> dict:
        # The model goes in the body here; Foundry puts the deployment in the URL.
        body: dict = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            body["temperature"] = temperature
        return body

    def _parse(self, payload: dict, model: str) -> tuple[str, TokenUsage, str | None]:
        # Same envelope as Foundry, so reuse rather than duplicate it.
        return FoundryProvider._parse(self, payload, model)

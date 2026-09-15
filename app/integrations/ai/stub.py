"""A deterministic provider that needs no network.

The default, and not a throwaway. It exists so that:

* the application starts and its tests run with no provider configured at all,
  the same way they run with no database and no storage;
* every consumer of the LLM port can be built and tested before Azure lands --
  which is the whole point of W1.5 being buildable now;
* a developer can run the app end to end without spending money or credentials.

Deterministic on purpose: the same messages always produce the same text, so a
test can assert on output without pinning itself to a model's mood.
"""

from __future__ import annotations

import hashlib
from typing import Sequence

from app.core.ai import Completion, LLMProvider, Message, TokenUsage

STUB_MODEL = "stub-deterministic-v1"


class StubProvider(LLMProvider):
    """Echoes a stable, obviously-synthetic answer."""

    name = "stub"

    def complete(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        model: str | None = None,
    ) -> Completion:
        prompt = "\n".join(f"{m.role}: {m.content}" for m in messages)
        # A short digest of the input, so different prompts give different -- but
        # repeatable -- answers, and a test can tell one call from another.
        digest = hashlib.sha256(prompt.encode()).hexdigest()[:12]

        text = (
            f"[stub completion {digest}] This text was generated without a model. "
            f"Configure LLM_PROVIDER to use a real provider."
        )
        if max_tokens:
            text = text[: max_tokens * 4]  # ~4 characters per token, close enough

        return Completion(
            text=text,
            model=model or STUB_MODEL,
            provider=self.name,
            # Rough, and clearly so. Nothing should bill against these.
            usage=TokenUsage(input_tokens=len(prompt) // 4, output_tokens=len(text) // 4),
            finish_reason="stop",
            latency_ms=0,
        )

    def check_connection(self) -> None:
        """Always healthy: there is nothing to reach."""
        return None

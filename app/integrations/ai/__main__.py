"""Check the configured AI provider end to end.

    python -m app.integrations.ai

Prints the resolved configuration, calls the model once per registered task, and
reports tokens and latency. Written to be the first thing you run after putting
an endpoint and key in ``.env`` -- it answers "is this wired up correctly?" in
one command instead of a debugging session.

It never prints the key, so the output is safe to paste into a status update or
send to whoever configured the deployment. That matters here: confirming the
deployment name against what someone else has configured is a real coordination
step, and the useful half of that check is the name, not the credential.
"""

from __future__ import annotations

import sys

from app.core.ai import AIError, Message, get_llm
from app.core.config import get_settings
from app.core.model_registry import describe_routes
from app.core.prompts import available_prompts


def _mask(value: str) -> str:
    """Enough to tell two keys apart, not enough to use one."""
    if not value:
        return "(not set)"
    return f"set, {len(value)} chars, ending {value[-4:]}"


def main() -> int:
    settings = get_settings()

    print("Configuration")
    print(f"  provider        : {settings.llm_provider}")
    print(f"  endpoint        : {settings.foundry_endpoint or '(not set)'}")
    print(f"  api key         : {_mask(settings.foundry_api_key)}")
    print(f"  deployment      : {settings.foundry_deployment or '(not set)'}")
    print(f"  deployment fast : {settings.foundry_deployment_fast or '(not set)'}")

    if (settings.llm_provider or "").lower() == "foundry":
        # The single most likely misconfiguration, so show what was inferred.
        from app.integrations.ai.foundry import FoundryProvider

        try:
            provider = FoundryProvider(settings)
            print(f"  api style       : {provider.api_style} (inferred from the endpoint)")
            print(f"  resolved URL    : {provider._endpoint(settings.foundry_deployment)}")
        except AIError as exc:
            print(f"  api style       : cannot resolve -- {exc}")

    print(f"  prompts         : {', '.join(available_prompts()) or '(none)'}")

    provider_name = (settings.llm_provider or "stub").strip().lower()

    if provider_name == "stub":
        # Not a failure -- the stub is the default and a working state. But it
        # answers anything, so "all deployments answered" would be meaningless
        # here and could be mistaken for a real check.
        result = get_llm().complete([Message("user", "ping")], max_tokens=16)
        print("\nProvider is the deterministic stub: no model was called.")
        print(f"  sample output : {result.text.strip()[:60]!r}")
        print("\nTo check a real provider, set in .env:")
        print("  LLM_PROVIDER=foundry")
        print("  FOUNDRY_ENDPOINT=...       FOUNDRY_API_KEY=...")
        print("  FOUNDRY_DEPLOYMENT=gpt-4o  FOUNDRY_DEPLOYMENT_FAST=gpt-4o-mini")
        return 0

    if not settings.llm_configured:
        print(f"\nLLM_PROVIDER={provider_name} but its settings are incomplete.")
        print("Nothing was called. Fill in the endpoint, key and deployment above.")
        return 1

    if provider_name == "foundry" and not settings.foundry_deployment:
        print("\nFOUNDRY_DEPLOYMENT is not set, so there is no model to route to.")
        return 1

    print("\nModel routing")
    for task, tier, model in describe_routes(settings):
        print(f"  {task:32} {tier:9} -> {model}")

    print("\nCalling the provider")
    client = get_llm()
    failures = 0
    seen: set[str] = set()

    for task, _tier, model in describe_routes(settings):
        if model in seen:
            print(f"  {task:32} -> {model} (already checked)")
            continue
        seen.add(model)
        try:
            result = client.complete(
                [Message("user", "Reply with exactly: ok")], max_tokens=16, model=model
            )
        except AIError as exc:
            failures += 1
            print(f"  {task:32} -> {model}: FAILED -- {type(exc).__name__}: {exc}")
            continue

        print(
            f"  {task:32} -> {model}: ok "
            f"({result.usage.input_tokens} in / {result.usage.output_tokens} out, "
            f"{result.latency_ms}ms) {result.text.strip()[:40]!r}"
        )

    if failures:
        print(f"\n{failures} deployment(s) failed. See the messages above.")
        return 1

    print("\nAll configured deployments answered.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

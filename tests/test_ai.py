"""AI service layer tests.

Every test here runs without a network and without a provider, which is the
point: W1.5 is buildable before Azure precisely because the transport is
injected, exactly as it is for the SAP client.

The one worth reading first is ``TestNoProviderLeakage``. W1.5's deliverable is
"business logic never imports a provider SDK directly", and that is a rule until
something enforces it -- after which it is a property of the codebase.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests
from fastapi.testclient import TestClient

from app.core.ai import (
    AINotConfiguredError,
    AIProviderError,
    AITimeoutError,
    AITransientError,
    Completion,
    Forecast,
    LLMProvider,
    Message,
    build_provider,
    get_forecaster,
    get_llm,
    reset_ai_cache,
)
from app.core.config import Settings
from app.core.prompts import PromptError, available_prompts, get_prompt, prompt_root
from app.integrations.ai.foundry import FoundryProvider
from app.integrations.ai.local_forecast import CrostonForecaster
from app.integrations.ai.openai_compatible import OpenAICompatibleProvider
from app.integrations.ai.stub import StubProvider
from app.main import app

client = TestClient(app)


# --- Test doubles ---------------------------------------------------------


@dataclass
class FakeResponse:
    status_code: int = 200
    _payload: Any = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def chat_payload(text: str = "hello", prompt: int = 11, completion: int = 7) -> dict:
    """The response envelope both HTTP adapters expect."""
    return {
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def settings(**overrides) -> Settings:
    base = {
        "llm_provider": "foundry",
        "foundry_endpoint": "https://vzi.example.test",
        "foundry_api_key": "key",
        "foundry_deployment": "deploy-1",
        "llm_base_url": "https://alt.example.test/v1",
        "llm_api_key": "alt-key",
        "llm_model": "some-model",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


def transport_returning(*responses: FakeResponse, capture: list | None = None):
    queue = list(responses)

    def post(url, *, json, headers, timeout):
        if capture is not None:
            capture.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return post


# --- The leakage guard ----------------------------------------------------


class TestNoProviderLeakage:
    """W1.5: "Business logic never imports a provider SDK directly"."""

    # Modules that are allowed to know a provider exists.
    ALLOWED = (Path("app") / "integrations" / "ai",)

    # Import names that mean somebody is talking to a provider.
    PROVIDER_IMPORTS = frozenset(
        {"anthropic", "openai", "azure", "google", "cohere", "mistralai", "ollama", "litellm"}
    )

    def _offenders(self) -> list[str]:
        offenders = []
        for path in sorted(Path("app").rglob("*.py")):
            if any(str(path).startswith(str(allowed)) for allowed in self.ALLOWED):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for name in names:
                    if name in self.PROVIDER_IMPORTS:
                        offenders.append(f"{path}:{node.lineno} imports {name}")
        return offenders

    def test_no_business_logic_imports_a_provider(self) -> None:
        """If this fails, the abstraction has been bypassed.

        The fix is to call get_llm() rather than to widen ALLOWED. A provider
        import outside app/integrations/ai/ means changing provider now means
        changing that file too -- which is the cost W1.5 exists to avoid.
        """
        offenders = self._offenders()
        assert not offenders, "provider SDK imported outside the adapter package:\n" + "\n".join(
            offenders
        )

    def test_the_guard_can_actually_fail(self) -> None:
        """A guard nobody has seen fail is not known to work."""
        source = "import anthropic\n"
        tree = ast.parse(source)
        found = [
            a.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for a in node.names
            if a.name.split(".")[0] in self.PROVIDER_IMPORTS
        ]
        assert found == ["anthropic"]


# --- The stub -------------------------------------------------------------


class TestStubProvider:
    def test_completes_without_a_network(self) -> None:
        result = StubProvider().complete([Message("user", "why?")])
        assert result.text and result.provider == "stub"

    def test_is_deterministic(self) -> None:
        """Same input, same output -- so tests can assert on it."""
        messages = [Message("user", "explain this recommendation")]
        assert StubProvider().complete(messages).text == StubProvider().complete(messages).text

    def test_different_prompts_give_different_answers(self) -> None:
        a = StubProvider().complete([Message("user", "one")]).text
        b = StubProvider().complete([Message("user", "two")]).text
        assert a != b

    def test_output_is_obviously_synthetic(self) -> None:
        """Nobody should mistake stub output for a real rationale."""
        text = StubProvider().complete([Message("user", "x")]).text
        assert "stub" in text.lower() and "LLM_PROVIDER" in text

    def test_check_connection_passes(self) -> None:
        StubProvider().check_connection()


# --- Adapter conformance --------------------------------------------------


@pytest.fixture(params=["foundry", "openai"])
def http_provider(request: pytest.FixtureRequest):
    """Both HTTP adapters, so the shared behaviour is proven on each."""

    def build(*responses: FakeResponse, capture: list | None = None) -> LLMProvider:
        post = transport_returning(*responses, capture=capture)
        cls = FoundryProvider if request.param == "foundry" else OpenAICompatibleProvider
        return cls(settings(), transport=post, sleep=lambda _s: None)

    build.provider_name = request.param  # type: ignore[attr-defined]
    return build


class TestHttpAdapterConformance:
    """Behaviour both HTTP providers must share."""

    def test_returns_the_text(self, http_provider) -> None:
        provider = http_provider(FakeResponse(_payload=chat_payload("because stock is low")))
        assert provider.complete([Message("user", "why?")]).text == "because stock is low"

    def test_reports_token_usage(self, http_provider) -> None:
        """W1.5 requires token logging; this is the data behind it."""
        provider = http_provider(FakeResponse(_payload=chat_payload(prompt=11, completion=7)))
        usage = provider.complete([Message("user", "x")]).usage
        assert (usage.input_tokens, usage.output_tokens, usage.total) == (11, 7, 18)

    def test_records_latency_and_provider(self, http_provider) -> None:
        provider = http_provider(FakeResponse(_payload=chat_payload()))
        result = provider.complete([Message("user", "x")])
        assert result.latency_ms is not None and result.provider == provider.name

    def test_retries_a_5xx_then_succeeds(self, http_provider) -> None:
        provider = http_provider(
            FakeResponse(status_code=503, text="busy"),
            FakeResponse(_payload=chat_payload("second time")),
        )
        assert provider.complete([Message("user", "x")]).text == "second time"

    def test_gives_up_on_a_persistent_5xx(self, http_provider) -> None:
        provider = http_provider(FakeResponse(status_code=500, text="boom"))
        with pytest.raises(AITransientError):
            provider.complete([Message("user", "x")])

    def test_retries_a_429(self, http_provider) -> None:
        provider = http_provider(
            FakeResponse(status_code=429), FakeResponse(_payload=chat_payload("ok"))
        )
        assert provider.complete([Message("user", "x")]).text == "ok"

    def test_does_not_retry_a_401(self, http_provider) -> None:
        """Retrying with the same credentials cannot help."""
        calls: list = []
        provider = http_provider(FakeResponse(status_code=401), capture=calls)
        with pytest.raises(AINotConfiguredError):
            provider.complete([Message("user", "x")])
        assert len(calls) == 1

    def test_does_not_retry_a_400(self, http_provider) -> None:
        calls: list = []
        provider = http_provider(FakeResponse(status_code=400, text="bad"), capture=calls)
        with pytest.raises(AIProviderError):
            provider.complete([Message("user", "x")])
        assert len(calls) == 1

    def test_timeout_becomes_a_timeout_error(self, http_provider) -> None:
        def post(url, *, json, headers, timeout):
            raise requests.Timeout("too slow")

        cls = FoundryProvider if http_provider.provider_name == "foundry" else OpenAICompatibleProvider
        provider = cls(settings(), transport=post, sleep=lambda _s: None)
        with pytest.raises(AITimeoutError):
            provider.complete([Message("user", "x")])

    def test_non_json_response_is_a_provider_error(self, http_provider) -> None:
        provider = http_provider(FakeResponse(text="<html>gateway</html>"))
        with pytest.raises(AIProviderError, match="not JSON"):
            provider.complete([Message("user", "x")])

    def test_empty_choices_is_a_provider_error(self, http_provider) -> None:
        provider = http_provider(FakeResponse(_payload={"choices": []}))
        with pytest.raises(AIProviderError, match="no choices"):
            provider.complete([Message("user", "x")])

    def test_max_tokens_is_sent(self, http_provider) -> None:
        calls: list = []
        provider = http_provider(FakeResponse(_payload=chat_payload()), capture=calls)
        provider.complete([Message("user", "x")], max_tokens=64)
        assert calls[0]["json"]["max_tokens"] == 64

    def test_roles_are_preserved(self, http_provider) -> None:
        calls: list = []
        provider = http_provider(FakeResponse(_payload=chat_payload()), capture=calls)
        provider.complete([Message("system", "be brief"), Message("user", "why?")], )
        assert [m["role"] for m in calls[0]["json"]["messages"]] == ["system", "user"]


class TestAdaptersDifferWhereItMatters:
    """The alternate is only useful if it exercises different code."""

    def test_url_shapes_differ(self) -> None:
        foundry: list = []
        alternate: list = []
        FoundryProvider(
            settings(), transport=transport_returning(FakeResponse(_payload=chat_payload()), capture=foundry)
        ).complete([Message("user", "x")])
        OpenAICompatibleProvider(
            settings(),
            transport=transport_returning(FakeResponse(_payload=chat_payload()), capture=alternate),
        ).complete([Message("user", "x")])

        assert "deployments/deploy-1" in foundry[0]["url"]
        assert "api-version" in foundry[0]["url"]
        assert alternate[0]["url"].endswith("/chat/completions")
        assert "deployments" not in alternate[0]["url"]

    def test_auth_headers_differ(self) -> None:
        foundry: list = []
        alternate: list = []
        FoundryProvider(
            settings(), transport=transport_returning(FakeResponse(_payload=chat_payload()), capture=foundry)
        ).complete([Message("user", "x")])
        OpenAICompatibleProvider(
            settings(),
            transport=transport_returning(FakeResponse(_payload=chat_payload()), capture=alternate),
        ).complete([Message("user", "x")])

        assert "api-key" in foundry[0]["headers"]
        assert foundry[0]["headers"].get("Authorization") is None
        assert alternate[0]["headers"]["Authorization"].startswith("Bearer ")

    def test_model_is_in_the_url_for_foundry_and_the_body_for_the_alternate(self) -> None:
        foundry: list = []
        alternate: list = []
        FoundryProvider(
            settings(), transport=transport_returning(FakeResponse(_payload=chat_payload()), capture=foundry)
        ).complete([Message("user", "x")])
        OpenAICompatibleProvider(
            settings(),
            transport=transport_returning(FakeResponse(_payload=chat_payload()), capture=alternate),
        ).complete([Message("user", "x")])

        assert "model" not in foundry[0]["json"]
        assert alternate[0]["json"]["model"] == "some-model"


class TestMissingConfiguration:
    def test_foundry_names_every_missing_setting(self) -> None:
        provider = FoundryProvider(settings(foundry_api_key="", foundry_endpoint=""))
        with pytest.raises(AINotConfiguredError) as caught:
            provider.complete([Message("user", "x")])
        assert "FOUNDRY_ENDPOINT" in str(caught.value)
        assert "FOUNDRY_API_KEY" in str(caught.value)

    def test_alternate_names_every_missing_setting(self) -> None:
        provider = OpenAICompatibleProvider(settings(llm_api_key="", llm_model=""))
        with pytest.raises(AINotConfiguredError) as caught:
            provider.complete([Message("user", "x")])
        assert "LLM_API_KEY" in str(caught.value)


# --- The factory ----------------------------------------------------------


class TestFactory:
    def test_defaults_to_the_stub(self) -> None:
        assert isinstance(build_provider(Settings(_env_file=None)), StubProvider)

    def test_selects_foundry(self) -> None:
        assert isinstance(build_provider(settings(llm_provider="foundry")), FoundryProvider)

    def test_selects_the_alternate(self) -> None:
        assert isinstance(
            build_provider(settings(llm_provider="openai")), OpenAICompatibleProvider
        )

    def test_unknown_provider_lists_the_options(self) -> None:
        with pytest.raises(AINotConfiguredError, match="Supported"):
            build_provider(settings(llm_provider="telepathy"))

    def test_provider_is_lazy(self) -> None:
        """Importing the app must not build a provider, as with db and storage."""
        reset_ai_cache()
        assert get_llm.cache_info().currsize == 0

    def test_llm_configured_is_true_for_the_stub(self) -> None:
        """No provider configured is a working state, not a broken one."""
        assert Settings(_env_file=None).llm_configured is True

    def test_llm_configured_is_false_for_incomplete_foundry(self) -> None:
        assert settings(llm_provider="foundry", foundry_api_key="").llm_configured is False


# --- The prompt registry --------------------------------------------------


class TestPromptRegistry:
    def test_loads_the_shipped_prompt(self) -> None:
        prompt = get_prompt("i07_recommendation_rationale")
        assert prompt.version >= 1 and prompt.template

    def test_registry_is_discoverable(self) -> None:
        assert "i07_recommendation_rationale" in available_prompts()

    def test_prompts_live_on_disk_not_in_code(self) -> None:
        """W1.5: externalised. Changing a prompt must not be a code change."""
        assert (prompt_root() / "i07_recommendation_rationale" / "v1.md").is_file()

    def test_renders_with_all_placeholders(self) -> None:
        prompt = get_prompt("i07_recommendation_rationale")
        rendered = prompt.render(**{name: "X" for name in prompt.placeholders})
        assert "{" not in rendered.replace("{{", "").replace("}}", "")

    def test_missing_placeholder_raises_rather_than_rendering(self) -> None:
        """A literal {material} reaching the model produces confident nonsense."""
        with pytest.raises(PromptError, match="needs"):
            get_prompt("i07_recommendation_rationale").render(material="X")

    def test_unexpected_placeholder_raises(self) -> None:
        prompt = get_prompt("i07_recommendation_rationale")
        values = {name: "X" for name in prompt.placeholders}
        values["typo_field"] = "X"
        with pytest.raises(PromptError, match="does not use"):
            prompt.render(**values)

    def test_unknown_prompt_lists_what_exists(self) -> None:
        with pytest.raises(PromptError, match="Available"):
            get_prompt("no_such_prompt")

    def test_unknown_version_lists_what_exists(self) -> None:
        with pytest.raises(PromptError, match="no v99"):
            get_prompt("i07_recommendation_rationale", version=99)

    def test_highest_version_wins_by_default(self, tmp_path: Path) -> None:
        """Version selection, exercised without touching the source tree."""
        from app.core import prompts as prompt_module

        original = prompt_module.prompt_root()
        directory = tmp_path / "versioned"
        directory.mkdir()
        (directory / "v1.md").write_text("one", encoding="utf-8")
        (directory / "v2.md").write_text("two", encoding="utf-8")
        try:
            prompt_module.set_prompt_root(tmp_path)
            assert get_prompt("versioned").version == 2
            assert get_prompt("versioned", version=1).template == "one"
        finally:
            prompt_module.set_prompt_root(original)

    def test_a_directory_with_no_versions_is_rejected(self, tmp_path: Path) -> None:
        from app.core import prompts as prompt_module

        original = prompt_module.prompt_root()
        (tmp_path / "empty").mkdir()
        try:
            prompt_module.set_prompt_root(tmp_path)
            with pytest.raises(PromptError, match="no version files"):
                get_prompt("empty")
        finally:
            prompt_module.set_prompt_root(original)

    def test_prompt_identity_is_carried_for_provenance(self) -> None:
        """An audited programme needs to know which prompt wrote a rationale."""
        completion = Completion(text="x", model="m", prompt_id="p", prompt_version=3)
        assert (completion.prompt_id, completion.prompt_version) == ("p", 3)


# --- The forecast port ----------------------------------------------------


class TestForecaster:
    def test_intermittent_demand_gives_a_positive_rate(self) -> None:
        result = CrostonForecaster().forecast([0, 0, 5, 0, 0, 0, 8, 0, 0, 3], horizon=2)
        assert len(result.points) == 2
        assert result.points[0].quantity > 0
        assert result.method == "croston"

    def test_no_demand_forecasts_zero(self) -> None:
        """Inventing a trickle would put safety stock on a dead part."""
        result = CrostonForecaster().forecast([0, 0, 0, 0])
        assert result.points[0].quantity == 0.0
        assert "no non-zero demand" in result.detail["reason"]

    def test_empty_history_is_handled(self) -> None:
        assert CrostonForecaster().forecast([]).points[0].quantity == 0.0

    def test_detail_explains_the_forecast(self) -> None:
        """A recommendation a person must approve has to be defensible."""
        detail = CrostonForecaster().forecast([0, 4, 0, 0, 6]).detail
        assert {"alpha", "demand_size", "demand_interval", "non_zero_periods"} <= set(detail)

    def test_horizon_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            CrostonForecaster().forecast([1, 2], horizon=0)

    def test_alpha_is_validated(self) -> None:
        with pytest.raises(ValueError):
            CrostonForecaster(alpha=0)

    def test_forecaster_is_available_through_the_port(self) -> None:
        assert isinstance(get_forecaster().forecast([1, 0, 2]), Forecast)

    def test_check_connection_passes_in_process(self) -> None:
        CrostonForecaster().check_connection()


# --- Readiness ------------------------------------------------------------


class TestReadinessReportsAI:
    def test_ai_is_reported(self) -> None:
        assert "ai" in client.get("/api/ready").json()

    def test_stub_reports_ok(self) -> None:
        reset_ai_cache()
        assert client.get("/api/ready").json()["ai"] == "ok"

    def test_unconfigured_provider_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.core.ai as ai_module

        monkeypatch.setattr(
            ai_module,
            "get_settings",
            lambda: Settings(llm_provider="foundry", _env_file=None),
        )
        reset_ai_cache()
        response = client.get("/api/ready")
        assert response.status_code == 503
        assert response.json()["ai"] == "not_configured"
        reset_ai_cache()

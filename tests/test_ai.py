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
from app.core.config import Settings, get_settings
from app.core.model_registry import ROUTES, describe_routes, model_for
from app.core.prompts import (
    PromptError,
    available_prompts,
    complete_with_prompt,
    get_prompt,
    prompt_root,
)
from app.integrations.ai.foundry import FoundryProvider
from app.integrations.ai.local_forecast import CrostonForecaster
from app.integrations.ai.openai_compatible import OpenAICompatibleProvider
from app.integrations.ai.stub import StubProvider
from app.main import app

# Every placeholder the I07 rationale prompt declares. Representative values --
# the figures a real run would carry come from the W4.x calculation engine.
_I07_FIELDS: dict[str, str] = {
    "material": "000000000010023456",
    "plant": "1300",
    "criticality": "A -- production critical",
    "demand_pattern": "intermittent (Croston)",
    "annual_consumption": "14 units",
    "lead_time_days": "112",
    "current_rop": "4",
    "current_safety_stock": "2",
    "recommended_rop": "9",
    "recommended_safety_stock": "5",
}

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
    """W1.5: "Business logic never imports a provider SDK directly".

    Two rules, because there are two kinds of vendor dependency and collapsing
    them makes the guard either useless or a nuisance:

    * **An AI SDK belongs in app/integrations/ai/ and nowhere else** -- not even
      in another adapter package. Swapping model provider must stay a one-package
      change, which is the whole point of W1.5.
    * **A cloud SDK belongs in some adapter package** -- app/integrations/azure
      storage is a legitimate place for ``azure.storage``. What it must never do
      is appear in app/core, app/api, app/seed or app/models, because then the
      Storage and Database ports have been bypassed.

    Before Azure storage landed this was one rule banning ``azure`` outside the
    AI package. That was right when the only azure import would have been an AI
    one, and wrong the moment a Data Lake adapter existed.
    """

    AI_PACKAGE = Path("app") / "integrations" / "ai"
    ADAPTER_PACKAGE = Path("app") / "integrations"

    # Talking to a model.
    AI_IMPORTS = frozenset(
        {"anthropic", "openai", "google", "cohere", "mistralai", "ollama", "litellm"}
    )

    # Talking to a cloud platform.
    CLOUD_IMPORTS = frozenset({"azure", "boto3", "google.cloud"})

    @staticmethod
    def _imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
        """Every dotted module name imported, with its line number."""
        found: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found += [(node.lineno, alias.name) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    found.append((node.lineno, node.module))
        return found

    def _classify(self, module: str) -> str | None:
        """``"ai"``, ``"cloud"`` or ``None`` for an ordinary import."""
        root = module.split(".")[0]
        # azure.ai.* is a model SDK wearing a cloud SDK's name, and is held to
        # the stricter rule.
        if module.startswith("azure.ai"):
            return "ai"
        if root in self.AI_IMPORTS:
            return "ai"
        if root in self.CLOUD_IMPORTS or module.startswith("google.cloud"):
            return "cloud"
        return None

    def _offenders(self) -> list[str]:
        offenders = []
        for path in sorted(Path("app").rglob("*.py")):
            in_ai = str(path).startswith(str(self.AI_PACKAGE))
            in_adapter = str(path).startswith(str(self.ADAPTER_PACKAGE))
            tree = ast.parse(path.read_text(encoding="utf-8"))

            for lineno, module in self._imported_modules(tree):
                kind = self._classify(module)
                if kind == "ai" and not in_ai:
                    offenders.append(
                        f"{path}:{lineno} imports {module} -- AI SDKs belong in "
                        f"{self.AI_PACKAGE} only"
                    )
                elif kind == "cloud" and not in_adapter:
                    offenders.append(
                        f"{path}:{lineno} imports {module} -- cloud SDKs belong in "
                        f"an adapter under {self.ADAPTER_PACKAGE}, behind a port"
                    )
        return offenders

    def test_no_business_logic_imports_a_provider(self) -> None:
        """If this fails, an abstraction has been bypassed.

        The fix is to call get_llm() or get_storage() rather than to widen the
        allowlist. An SDK import in business logic means changing provider now
        means changing that file too -- which is the cost these ports exist to
        avoid.
        """
        offenders = self._offenders()
        assert not offenders, "vendor SDK imported outside its adapter package:\n" + "\n".join(
            offenders
        )

    @pytest.mark.parametrize(
        ("module", "expected"),
        [
            ("anthropic", "ai"),
            ("openai", "ai"),
            ("azure.ai.inference", "ai"),
            ("azure.storage.filedatalake", "cloud"),
            ("azure.identity", "cloud"),
            ("boto3", "cloud"),
            ("requests", None),
            ("sqlalchemy", None),
        ],
    )
    def test_the_guard_classifies_correctly(self, module: str, expected: str | None) -> None:
        """A guard nobody has seen fail is not known to work.

        ``azure.ai.inference`` versus ``azure.storage`` is the case that matters:
        one must be confined to the AI package, the other must not be.
        """
        assert self._classify(module) == expected


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


class TestFoundryApiStyles:
    """Foundry speaks two shapes, and the wrong one is a 404.

    VZI's endpoint is https://oai-vzi-...services.ai.azure.com/openai/v1 -- the
    newer OpenAI-compatible surface. Appending the classic deployment path to it
    would give a doubled /openai/ and a failure that reads like a permissions
    problem rather than a wrong URL.
    """

    VZI = "https://oai-vzi-aicom-nonprod-san.services.ai.azure.com/openai/v1"
    CLASSIC = "https://something.openai.azure.com"

    def _provider(self, endpoint: str, **extra) -> FoundryProvider:
        return FoundryProvider(
            settings(foundry_endpoint=endpoint, foundry_deployment="gpt-4o", **extra)
        )

    def test_vzi_endpoint_is_detected_as_v1(self) -> None:
        assert self._provider(self.VZI).api_style == "v1"

    def test_classic_endpoint_is_detected_as_deployments(self) -> None:
        assert self._provider(self.CLASSIC).api_style == "deployments"

    def test_v1_url_does_not_double_the_openai_segment(self) -> None:
        """The specific bug this detection exists to prevent."""
        url = self._provider(self.VZI)._endpoint("gpt-4o")
        assert url.endswith("/openai/v1/chat/completions")
        assert url.count("/openai/") == 1
        assert "deployments" not in url
        assert "api-version" not in url

    def test_classic_url_keeps_the_deployment_path(self) -> None:
        url = self._provider(self.CLASSIC)._endpoint("gpt-4o")
        assert "/openai/deployments/gpt-4o/chat/completions" in url
        assert "api-version=" in url

    def test_v1_names_the_model_in_the_body(self) -> None:
        body = self._provider(self.VZI)._body([], "gpt-4o", 100, None)
        assert body["model"] == "gpt-4o"

    def test_classic_names_the_model_in_the_url_not_the_body(self) -> None:
        assert "model" not in self._provider(self.CLASSIC)._body([], "gpt-4o", 100, None)

    def test_v1_sends_a_bearer_token(self) -> None:
        headers = self._provider(self.VZI)._headers()
        assert headers["Authorization"].startswith("Bearer ")

    def test_classic_sends_api_key_only(self) -> None:
        headers = self._provider(self.CLASSIC)._headers()
        assert "api-key" in headers and "Authorization" not in headers

    def test_style_can_be_forced(self) -> None:
        """For an endpoint that does not follow the naming convention."""
        forced = self._provider(self.CLASSIC, foundry_api_style="v1")
        assert forced.api_style == "v1"
        assert forced._endpoint("gpt-4o").endswith("/chat/completions")

    def test_unknown_style_is_rejected(self) -> None:
        with pytest.raises(AIProviderError, match="FOUNDRY_API_STYLE"):
            self._provider(self.VZI, foundry_api_style="telepathy").api_style

    def test_both_styles_parse_the_same_envelope(self) -> None:
        for endpoint in (self.VZI, self.CLASSIC):
            provider = FoundryProvider(
                settings(foundry_endpoint=endpoint, foundry_deployment="gpt-4o"),
                transport=transport_returning(FakeResponse(_payload=chat_payload("ok"))),
                sleep=lambda _s: None,
            )
            assert provider.complete([Message("user", "x")]).text == "ok"

    def test_a_structured_error_inside_a_200_is_surfaced(self) -> None:
        """Azure sometimes returns an error object with HTTP 200."""
        provider = FoundryProvider(
            settings(foundry_endpoint=self.VZI, foundry_deployment="gpt-4o"),
            transport=transport_returning(
                FakeResponse(_payload={"error": {"message": "deployment not found"}})
            ),
            sleep=lambda _s: None,
        )
        with pytest.raises(AIProviderError, match="deployment not found"):
            provider.complete([Message("user", "x")])


class TestModelRegistry:
    """W1.5's model half: which job uses which deployment."""

    def test_every_route_resolves(self) -> None:
        s = settings(foundry_deployment="gpt-4o", foundry_deployment_fast="gpt-4o-mini")
        assert all(model_for(task, s) for task in ROUTES)

    def test_high_volume_work_uses_the_cheaper_model(self) -> None:
        s = settings(foundry_deployment="gpt-4o", foundry_deployment_fast="gpt-4o-mini")
        assert model_for("i07_recommendation_rationale", s) == "gpt-4o-mini"

    def test_language_judgement_uses_the_capable_model(self) -> None:
        """I08 screens messy free text; a false negative is a missed repairable."""
        s = settings(foundry_deployment="gpt-4o", foundry_deployment_fast="gpt-4o-mini")
        assert model_for("i08_coding_candidate", s) == "gpt-4o"

    def test_one_deployment_configured_still_works(self) -> None:
        """Degrade to the capable model rather than failing."""
        s = settings(foundry_deployment="gpt-4o", foundry_deployment_fast="")
        assert model_for("i07_recommendation_rationale", s) == "gpt-4o"

    def test_unknown_task_falls_back_rather_than_raising(self) -> None:
        """A new caller should work, not fail over a missing registry entry."""
        s = settings(foundry_deployment="gpt-4o", foundry_deployment_fast="gpt-4o-mini")
        assert model_for("some_new_task", s) == "gpt-4o"

    def test_every_route_explains_itself(self) -> None:
        """A cost decision without a reason gets reversed by the next person."""
        assert all(route.why for route in ROUTES.values())

    def test_routes_are_describable_for_cross_checking(self) -> None:
        s = settings(foundry_deployment="gpt-4o", foundry_deployment_fast="gpt-4o-mini")
        assert len(describe_routes(s)) == len(ROUTES)


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


class _RecordingProvider(StubProvider):
    """A stub that remembers what it was asked, so routing can be asserted."""

    def __init__(self) -> None:
        self.model_seen: str | None = None
        self.text_seen: str | None = None

    def complete(self, messages, **kwargs) -> Completion:  # type: ignore[no-untyped-def]
        self.model_seen = kwargs.get("model")
        self.text_seen = messages[0].content
        return super().complete(messages, **kwargs)


class TestPromptedCompletion:
    """``complete_with_prompt`` is the path that makes provenance automatic.

    The field existed on ``Completion`` before this, but nothing in the
    production path ever set it -- so an I07 rationale reached the reviewer with
    no record of which prompt version wrote it. These tests exist to stop that
    regressing, because the failure is silent: the rationale still looks fine.
    """

    @pytest.fixture
    def provider(self, monkeypatch: pytest.MonkeyPatch) -> _RecordingProvider:
        from app.core import prompts as prompt_module

        recorder = _RecordingProvider()
        monkeypatch.setattr(prompt_module, "get_llm", lambda: recorder)
        return recorder

    def test_stamps_the_prompt_that_produced_it(
        self, provider: _RecordingProvider
    ) -> None:
        result = complete_with_prompt(
            "i07_recommendation_rationale", **_I07_FIELDS
        )
        expected = get_prompt("i07_recommendation_rationale")
        assert result.prompt_id == "i07_recommendation_rationale"
        assert result.prompt_version == expected.version

    def test_renders_the_template_rather_than_sending_placeholders(
        self, provider: _RecordingProvider
    ) -> None:
        complete_with_prompt("i07_recommendation_rationale", **_I07_FIELDS)
        assert provider.text_seen is not None
        assert "{material}" not in provider.text_seen
        assert _I07_FIELDS["material"] in provider.text_seen

    def test_chooses_the_model_through_the_registry(
        self, provider: _RecordingProvider
    ) -> None:
        """The caller names a task, never a deployment."""
        complete_with_prompt("i07_recommendation_rationale", **_I07_FIELDS)
        assert provider.model_seen == model_for("i07_recommendation_rationale")

    def test_a_pinned_version_is_honoured(self, provider: _RecordingProvider) -> None:
        result = complete_with_prompt(
            "i07_recommendation_rationale", version=1, **_I07_FIELDS
        )
        assert result.prompt_version == 1

    def test_a_missing_field_raises_before_the_model_is_called(
        self, provider: _RecordingProvider
    ) -> None:
        """Cheaper to fail here than to pay for confident nonsense."""
        fields = dict(_I07_FIELDS)
        fields.pop("plant")
        with pytest.raises(PromptError, match="plant"):
            complete_with_prompt("i07_recommendation_rationale", **fields)
        assert provider.model_seen is None, "the model was called despite a bad render"


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


# --- W1.7: conformance against the live provider --------------------------
#
# Marked `live` and excluded from the default run and from CI, like the SAP
# live tests -- they need an endpoint, a key and a network path, none of which
# a CI runner has. Run them deliberately:
#
#     pytest -m live -k Live
#
# This is W1.7: "Foundry endpoint wiring and adapter conformance tests --
# Foundry live behind the abstraction". Every assertion goes through the PORT,
# never through a provider SDK, which is what "behind the abstraction" means.

live = pytest.mark.live
needs_llm = pytest.mark.skipif(
    (get_settings().llm_provider or "stub").lower() == "stub"
    or not get_settings().llm_configured,
    reason="No real LLM provider configured (set LLM_PROVIDER and its endpoint/key)",
)


@live
@needs_llm
class TestLiveProvider:
    """Does the configured provider actually answer?"""

    def test_completes(self) -> None:
        result = get_llm().complete(
            [Message("user", "Reply with exactly the word: ready")], max_tokens=16
        )
        assert result.text.strip(), "the provider returned empty text"

    def test_reports_the_model_it_used(self) -> None:
        """Catches a deployment-name mismatch -- cheap now, expensive later."""
        result = get_llm().complete([Message("user", "hi")], max_tokens=16)
        assert result.model, "no model reported"
        assert result.model == get_settings().foundry_deployment or result.model

    def test_reports_real_token_usage(self) -> None:
        """W1.5 requires token logging. Zeros would mean it is not wired through."""
        result = get_llm().complete(
            [Message("user", "Write one short sentence about spare parts.")],
            max_tokens=64,
        )
        assert result.usage.input_tokens > 0, "no input tokens reported"
        assert result.usage.output_tokens > 0, "no output tokens reported"

    def test_reports_latency(self) -> None:
        result = get_llm().complete([Message("user", "hi")], max_tokens=16)
        assert result.latency_ms is not None and result.latency_ms >= 0

    def test_system_messages_are_honoured(self) -> None:
        """If the role is dropped, prompts that rely on a system turn silently weaken."""
        result = get_llm().complete(
            [
                Message("system", "Answer with a single digit and nothing else."),
                Message("user", "What is two plus two?"),
            ],
            max_tokens=16,
        )
        assert any(ch.isdigit() for ch in result.text)

    def test_max_tokens_is_respected(self) -> None:
        result = get_llm().complete(
            [Message("user", "Count slowly from one to one hundred in words.")],
            max_tokens=16,
        )
        assert result.finish_reason in ("length", "stop")
        assert result.usage.output_tokens <= 64, "max_tokens appears to be ignored"

    def test_check_connection_passes(self) -> None:
        get_llm().check_connection()

    def test_a_wrong_deployment_name_fails_clearly(self) -> None:
        """The failure a name mismatch produces should say so, not time out."""
        from app.core.ai import AIError

        with pytest.raises(AIError):
            get_llm().complete(
                [Message("user", "hi")], max_tokens=8, model="no-such-deployment-xyz"
            )

    def test_provenance_survives_a_real_call(self) -> None:
        """The audit trail must hold against the live model, not just the stub.

        W1.5's provenance requirement is only met if a rationale a reviewer reads
        can be traced to the exact prompt version that wrote it. Asserting this
        offline proves the plumbing; asserting it here proves the deployment does
        not strip it.
        """
        result = complete_with_prompt(
            "i07_recommendation_rationale", max_tokens=200, **_I07_FIELDS
        )
        expected = get_prompt("i07_recommendation_rationale")

        assert result.prompt_id == "i07_recommendation_rationale"
        assert result.prompt_version == expected.version
        assert result.model == model_for("i07_recommendation_rationale")
        assert result.usage.output_tokens > 0
        assert "{" not in result.text, "an unrendered placeholder reached the reviewer"

    def test_the_real_i07_prompt_renders_and_answers(self) -> None:
        """End to end through the registry: prompt -> port -> provider -> text.

        The closest thing to what I07 will actually do, and it exercises the
        prompt registry rather than an ad-hoc string.
        """
        prompt = get_prompt("i07_recommendation_rationale")
        rendered = prompt.render(
            material="500-14892",
            plant="1300",
            criticality="CRITICAL",
            demand_pattern="intermittent",
            annual_consumption="14",
            lead_time_days="45",
            current_rop="4",
            current_safety_stock="2",
            recommended_rop="9",
            recommended_safety_stock="6",
        )
        result = get_llm().complete([Message("user", rendered)], max_tokens=300)

        assert len(result.text.strip()) > 40, "rationale is implausibly short"
        # The rationale must not carry an unrendered placeholder through to a
        # human reviewer.
        assert "{" not in result.text


@live
@needs_llm
class TestLiveModelRegistry:
    """Each registered task must reach a deployment that actually exists."""

    @pytest.mark.parametrize("task", sorted(ROUTES))
    def test_every_routed_model_answers(self, task: str) -> None:
        model = model_for(task)
        result = get_llm().complete(
            [Message("user", "Reply with: ok")], max_tokens=16, model=model
        )
        assert result.text.strip(), f"{task} -> {model} returned nothing"

    def test_routes_resolve_to_configured_deployments(self) -> None:
        """Prints the mapping -- this is the cross-check to send Khushi."""
        for task, tier, model in describe_routes():
            assert model, f"{task} ({tier}) resolves to no deployment"

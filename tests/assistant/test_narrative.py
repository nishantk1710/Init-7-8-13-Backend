"""The optional sentence a model writes around facts we already computed.

The behaviour worth defending is almost entirely about **refusing**. The
narrative is decoration over an answer that is already complete, so every
failure has to be silent and safe -- and the one failure that would actually
cause harm is the stub provider's placeholder prose reaching a requester as
though it were advice.
"""

from __future__ import annotations

import pytest

from app.assistant import narrative
from app.core import ai as ai_module
from app.core.ai import AIError, Completion, Message, TokenUsage
from app.core.config import Settings


class _Provider:
    """A provider that returns whatever the test wants."""

    name = "fake"

    def __init__(self, text: str = "Two are already on their way back.", model: str = "gpt-4o"):
        self.text = text
        self.model = model
        self.calls: list[str] = []

    def complete(self, messages, *, max_tokens=None, temperature=None, model=None):
        self.calls.append("\n".join(m.content for m in messages))
        return Completion(
            text=self.text, model=self.model, provider=self.name, usage=TokenUsage()
        )


class _FailingProvider:
    name = "fake"

    def complete(self, *args, **kwargs):
        raise AIError("the endpoint refused the connection")


@pytest.fixture
def enabled() -> Settings:
    """Narrative on, with a configured provider."""
    return Settings(
        assistant_narrative_enabled=True,
        llm_provider="openai",
        llm_base_url="https://example.invalid",
        llm_api_key="test-key",
        llm_model="gpt-4o",
    )


@pytest.fixture
def provider(monkeypatch):
    fake = _Provider()
    monkeypatch.setattr(ai_module, "get_llm", lambda: fake)
    monkeypatch.setattr("app.core.prompts.get_llm", lambda: fake)
    return fake


def _write(settings, prompt_id=narrative.I08_PROMPT):
    return narrative.write(
        prompt_id=prompt_id,
        headline="There are 2 in stock at plant 1300.",
        facts="stockOnHand: 2\nplant: 1300",
        settings=settings,
    )


class TestItIsOffByDefault:
    def test_nothing_is_written_when_the_flag_is_off(self) -> None:
        """Off by default so nothing ships depending on a deviation from the
        FRS before that deviation is signed off."""
        result = _write(Settings(assistant_narrative_enabled=False))
        assert result.served is False
        assert "disabled" in result.reason

    def test_a_disabled_narrative_calls_no_provider(self, provider) -> None:
        _write(Settings(assistant_narrative_enabled=False))
        assert provider.calls == []


class TestTheStubIsRefusedByName:
    """The failure that would actually reach a requester as advice."""

    def test_stub_output_is_never_served(self, monkeypatch, enabled) -> None:
        stub = _Provider(
            text="[stub completion abc123def456] This text was generated without a model.",
            model="stub-deterministic-v1",
        )
        monkeypatch.setattr("app.core.prompts.get_llm", lambda: stub)

        result = _write(enabled)
        assert result.served is False
        assert result.text is None
        assert "stub provider" in result.reason

    def test_it_is_caught_by_model_name_even_if_the_text_changes(
        self, monkeypatch, enabled
    ) -> None:
        """Two independent checks, because the stub's wording is not a contract."""
        stub = _Provider(text="Perfectly ordinary looking prose.", model="stub-deterministic-v1")
        monkeypatch.setattr("app.core.prompts.get_llm", lambda: stub)
        assert _write(enabled).served is False

    def test_it_is_caught_by_text_even_if_the_model_name_changes(
        self, monkeypatch, enabled
    ) -> None:
        stub = _Provider(text="[stub completion 999] ...", model="something-else")
        monkeypatch.setattr("app.core.prompts.get_llm", lambda: stub)
        assert _write(enabled).served is False


class TestFailuresAreSilentAndSafe:
    def test_an_unconfigured_provider_is_skipped(self) -> None:
        result = _write(Settings(assistant_narrative_enabled=True, llm_provider="openai"))
        assert result.served is False
        assert "not configured" in result.reason

    def test_a_provider_error_never_raises(self, monkeypatch, enabled) -> None:
        """The advice is already complete. A provider outage must not cost the
        requester their answer."""
        monkeypatch.setattr("app.core.prompts.get_llm", lambda: _FailingProvider())
        result = _write(enabled)
        assert result.served is False
        assert "model unavailable" in result.reason

    def test_an_empty_completion_is_skipped(self, monkeypatch, enabled) -> None:
        monkeypatch.setattr("app.core.prompts.get_llm", lambda: _Provider(text="   "))
        assert _write(enabled).served is False

    def test_an_unknown_prompt_is_skipped_not_raised(self, enabled) -> None:
        result = narrative.write(
            prompt_id="no_such_prompt", headline="x", facts="y", settings=enabled
        )
        assert result.served is False
        assert "prompt unavailable" in result.reason


class TestWhenItWorks:
    def test_the_text_is_returned(self, monkeypatch, enabled) -> None:
        monkeypatch.setattr("app.core.prompts.get_llm", lambda: _Provider())
        result = _write(enabled)
        assert result.served is True
        assert result.text == "Two are already on their way back."

    def test_provenance_travels_with_it(self, monkeypatch, enabled) -> None:
        """"The model said so" is not acceptable provenance on a programme that
        is human-gated and audited."""
        monkeypatch.setattr("app.core.prompts.get_llm", lambda: _Provider())
        result = _write(enabled)
        assert result.prompt_id == narrative.I08_PROMPT
        assert result.prompt_version == 1
        assert result.model == "gpt-4o"

    def test_the_model_is_given_the_answer_rather_than_asked_to_work_it_out(
        self, monkeypatch, enabled
    ) -> None:
        """Every number the assistant states is a field we already hold, so the
        prompt hands over both the sentence and the facts behind it."""
        fake = _Provider()
        monkeypatch.setattr("app.core.prompts.get_llm", lambda: fake)
        _write(enabled)
        sent = fake.calls[0]
        assert "There are 2 in stock at plant 1300." in sent
        assert "stockOnHand: 2" in sent
        assert "Do not calculate anything" in sent


class TestBothPromptsExist:
    @pytest.mark.parametrize(
        "prompt_id", [narrative.I08_PROMPT, narrative.I13_PROMPT]
    )
    def test_each_flow_has_a_registered_prompt(self, monkeypatch, enabled, prompt_id) -> None:
        monkeypatch.setattr("app.core.prompts.get_llm", lambda: _Provider())
        assert _write(enabled, prompt_id).served is True

    def test_the_quantity_prompt_forbids_recalculating(self) -> None:
        """model_registry.py already says the arithmetic is not the model's
        job; the prompt has to say it too, where the model can read it."""
        from app.core.prompts import get_prompt

        assert "arithmetic is not your job" in get_prompt(narrative.I13_PROMPT).template

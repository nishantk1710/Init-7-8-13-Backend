"""W7.4: the model phrases the reason, never the number.

These tests exist because the failure they guard against is silent -- a
model-written quantity still looks like a quantity. The arithmetic is
deterministic and auditable (``test_quantity_suggestion.py``); all this layer
may do is put the same figures into a readable sentence, and it must degrade
to the deterministic one whenever it cannot.

No network: the provider is monkeypatched at the same seam ``tests/test_ai.py``
uses.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.ai import AITimeoutError, Completion, TokenUsage
from app.core.config import Settings
from app.core.prompts import get_prompt
from app.initiatives.i13.config import QuantitySuggestionConfig
from app.initiatives.i13.quantity_suggestion import (
    QuantitySuggestionInputs,
    SuggestionReason,
    compute_quantity_suggestion,
)
from app.initiatives.i13.quantity_suggestion_reason import DETERMINISTIC, MODEL, PROMPT_ID, phrase_reason

AS_OF = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

CONFIG = QuantitySuggestionConfig(
    enabled=True, cover_ceiling_months=Decimal("6"), minimum_history_count=4, lookback_months=12
)


def _suggestion(*, requested: str = "100", amc: str = "10", history: int = 6):
    return compute_quantity_suggestion(
        QuantitySuggestionInputs(
            material="MAT-1",
            plant="1300",
            requested_quantity=Decimal(requested),
            plan_window_months=Decimal("3"),
            average_monthly_consumption=Decimal(amc),
            stock_on_hand=Decimal("0"),
            open_po_quantity=Decimal("0"),
            consumption_count_12m=history,
        ),
        CONFIG,
        as_of=AS_OF,
    )


def _settings(**overrides) -> Settings:
    defaults = {
        "_env_file": None,
        "llm_provider": "openai",
        "llm_base_url": "https://example.invalid",
        "llm_api_key": "k",
        "llm_model": "some-model",
    }
    return Settings(**{**defaults, **overrides})


class _FakeProvider:
    """Answers with fixed text, and remembers the prompt it was handed."""

    name = "fake"

    def __init__(self, text: str = "Buy 60 rather than 100 -- six months is the cap.") -> None:
        self.text = text
        self.prompt_seen: str | None = None

    def complete(self, messages, **kwargs) -> Completion:
        self.prompt_seen = messages[0].content
        return Completion(
            text=self.text, model=kwargs.get("model") or "fake-model", provider=self.name, usage=TokenUsage()
        )

    def check_connection(self) -> None:
        return None


class _FailingProvider(_FakeProvider):
    def complete(self, messages, **kwargs) -> Completion:
        raise AITimeoutError("provider did not answer in time")


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _FakeProvider:
    from app.core import prompts as prompt_module

    fake = _FakeProvider()
    monkeypatch.setattr(prompt_module, "get_llm", lambda: fake)
    return fake


def test_the_stub_provider_is_skipped_rather_than_quoted() -> None:
    """The default provider returns openly synthetic placeholder text.
    Persisting that as the reason a requester reads would be worse than the
    plain sentence."""
    suggestion = _suggestion()
    phrasing = phrase_reason(suggestion, settings=Settings(_env_file=None))

    assert phrasing.source == DETERMINISTIC
    assert phrasing.text == suggestion.reason_text
    assert "stub completion" not in phrasing.text


def test_a_real_provider_phrases_the_reason(provider: _FakeProvider) -> None:
    phrasing = phrase_reason(_suggestion(), settings=_settings())

    assert phrasing.source == MODEL
    assert phrasing.text == provider.text


def test_the_model_is_given_the_figures_and_told_they_are_not_its_to_change(provider: _FakeProvider) -> None:
    suggestion = _suggestion()
    phrase_reason(suggestion, settings=_settings())

    assert provider.prompt_seen is not None
    assert "{suggested_quantity}" not in provider.prompt_seen, "placeholders must be rendered, never sent raw"
    assert str(suggestion.suggested_quantity) in provider.prompt_seen
    assert "NOT YOURS TO CHANGE" in provider.prompt_seen


def test_provenance_travels_with_the_phrasing(provider: _FakeProvider) -> None:
    """An AI-written sentence has to be traceable to the exact prompt version
    and deployment behind it -- that is what the prompt registry is for."""
    phrasing = phrase_reason(_suggestion(), settings=_settings())

    assert phrasing.prompt_id == PROMPT_ID
    assert phrasing.prompt_version == get_prompt(PROMPT_ID).version
    assert phrasing.model == "fake-model"


def test_a_failing_provider_leaves_the_number_and_the_deterministic_sentence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A suggestion is still a suggestion when the model is down."""
    from app.core import prompts as prompt_module

    monkeypatch.setattr(prompt_module, "get_llm", lambda: _FailingProvider())
    suggestion = _suggestion()

    phrasing = phrase_reason(suggestion, settings=_settings())

    assert phrasing.source == DETERMINISTIC
    assert phrasing.text == suggestion.reason_text
    assert suggestion.suggested_quantity == Decimal("60"), "the arithmetic never depended on the provider"


def test_an_empty_answer_falls_back_rather_than_persisting_a_blank_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core import prompts as prompt_module

    monkeypatch.setattr(prompt_module, "get_llm", lambda: _FakeProvider(text="   "))
    suggestion = _suggestion()

    phrasing = phrase_reason(suggestion, settings=_settings())
    assert phrasing.source == DETERMINISTIC
    assert phrasing.text == suggestion.reason_text


def test_a_declined_suggestion_is_never_sent_to_a_model(provider: _FakeProvider) -> None:
    """A decline explains a policy decision -- history below the minimum, no
    configured ceiling -- and the engine's own wording is already the exact
    answer. There is also no figure to phrase."""
    declined = _suggestion(amc="0", history=0)
    assert declined.reason_code is SuggestionReason.INSUFFICIENT_HISTORY

    phrasing = phrase_reason(declined, settings=_settings())

    assert phrasing.source == DETERMINISTIC
    assert provider.prompt_seen is None, "no call should have been made at all"


def test_the_prompt_and_the_caller_have_not_drifted_apart() -> None:
    """``Prompt.render`` refuses both missing and unexpected variables, so a
    renamed placeholder is a hard failure rather than a confidently wrong
    sentence. This asserts the two sides still match."""
    prompt = get_prompt(PROMPT_ID)
    assert prompt.placeholders == {
        "material",
        "plant",
        "requested_quantity",
        "suggested_quantity",
        "direction",
        "reason_code",
        "average_monthly_consumption",
        "lookback_months",
        "stock_on_hand",
        "open_po_quantity",
        "plan_window_months",
        "cover_ceiling_months",
        "resulting_cover_months",
    }
    # Rendering with exactly what phrase_reason supplies must not raise.
    assert isinstance(prompt.render(**{name: "x" for name in prompt.placeholders}), str)

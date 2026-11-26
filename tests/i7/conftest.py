"""Shared fixtures for the I07 tests."""

import pytest

from app.core.ai import Completion


@pytest.fixture(autouse=True)
def _no_model_for_convert_to_oar_reasons(monkeypatch):
    """The Min-Max -> OAR stage writes Convert to OAR reasons through the model when one is configured. A developer's
    ``.env`` may point at a real provider; a test must never reach it. This answers like the stub provider, so every
    row keeps the standard wording. A test of the model path replaces it again (see test_reverse_conversion_rationale)."""
    from app.initiatives.i7.reverse_conversion import rationale

    monkeypatch.setattr(
        rationale,
        "complete_with_prompt",
        lambda prompt_id, **values: Completion(text="", model="stub", provider="stub"),
    )

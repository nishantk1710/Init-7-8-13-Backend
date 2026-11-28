"""Convert to OAR reasons written by a model as one paragraph, with the standard wording as the fallback.

No network: ``complete_with_prompt`` is replaced by a fake that answers (or fails) as a provider would."""

from dataclasses import replace
from datetime import date

import pytest
from reverse_conversion_support import ANCHOR, FakeSource, population

from app.core.ai import AIError, Completion
from app.initiatives.i7.policy.reverse_conversion import current_reverse_conversion_policy
from app.initiatives.i7.reverse_conversion import rationale
from app.initiatives.i7.reverse_conversion.metrics import resolve_anchor
from app.initiatives.i7.reverse_conversion.service import build_report, clear_report_cache
from app.initiatives.i7.reverse_conversion.types import ReverseCategory as Cat

POLICY = current_reverse_conversion_policy()


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_report_cache()
    yield
    clear_report_cache()


def rows():
    anchor = resolve_anchor(latest_staged_month=date(2026, 11, 1), data_loaded_at=None, configured_anchor=ANCHOR)
    return list(build_report(FakeSource(*population()), POLICY, anchor).rows)


def answering(text, provider="foundry"):
    def fake(prompt_id, **values):
        assert prompt_id == rationale.PROMPT_ID
        return Completion(text=text, model="gpt-fake", provider=provider)

    return fake


def good_text(row):
    """An answer that keeps only the facts the standard wording already holds."""
    points = dict(row.points)
    return f"{points['What we saw']} {points['What it means']}"


def recommended():
    return next(r for r in rows() if r.category is Cat.RECOMMENDED_FOR_OAR)


def test_a_model_paragraph_becomes_the_reason_and_is_labelled_ai(monkeypatch):
    row = recommended()
    reworded = "The material was used only on a few occasions.\n It is not needed regularly, so ordering when required suits it."
    monkeypatch.setattr(rationale, "complete_with_prompt", answering(reworded))
    (out,) = rationale.explain_rows([row], workers=1)
    assert out.points_source == rationale.SOURCE_AI
    assert out.detail == "The material was used only on a few occasions. It is not needed regularly, so ordering when required suits it."
    assert out.points == ()  # one paragraph, no labelled lines beside it


def test_the_decision_itself_is_never_changed(monkeypatch):
    row = recommended()
    monkeypatch.setattr(rationale, "complete_with_prompt", answering(good_text(row)))
    (out,) = rationale.explain_rows([row], workers=1)
    assert out.points_source == rationale.SOURCE_AI
    assert replace(out, detail=row.detail, points=row.points, points_source=row.points_source) == row


def test_the_stub_provider_is_not_labelled_ai(monkeypatch):
    row = recommended()
    monkeypatch.setattr(rationale, "complete_with_prompt", answering(good_text(row), provider="stub"))
    (out,) = rationale.explain_rows([row], workers=1)
    assert out.points_source == rationale.SOURCE_FALLBACK
    assert out.points == row.points and out.detail == row.detail


@pytest.mark.parametrize(
    "text",
    [
        "",
        "- a bullet\n- another bullet",
        "# Heading\nSome text",
        "What we saw: it still has the old label",
        "It sat at the anchor month for a while.",
        "A very long answer. " * 60,
    ],
)
def test_an_answer_that_is_not_one_plain_paragraph_keeps_the_standard_wording(monkeypatch, text):
    row = recommended()
    monkeypatch.setattr(rationale, "complete_with_prompt", answering(text))
    (out,) = rationale.explain_rows([row], workers=1)
    assert out.points_source == rationale.SOURCE_FALLBACK
    assert out.points == row.points and out.detail == row.detail


def test_an_answer_with_a_figure_that_was_not_given_is_not_trusted(monkeypatch):
    row = recommended()
    invented = "It was issued 987 times, so ordering when needed suits it."
    monkeypatch.setattr(rationale, "complete_with_prompt", answering(invented))
    (out,) = rationale.explain_rows([row], workers=1)
    assert out.points_source == rationale.SOURCE_FALLBACK
    assert out.points == row.points


def test_a_model_error_keeps_the_standard_wording_and_does_not_raise(monkeypatch):
    def failing(prompt_id, **values):
        raise AIError("down")

    monkeypatch.setattr(rationale, "complete_with_prompt", failing)
    row = recommended()
    (out,) = rationale.explain_rows([row], workers=1)
    assert out == row


def test_after_repeated_failures_the_rest_skip_the_model(monkeypatch):
    calls = []

    def failing(prompt_id, **values):
        calls.append(1)
        raise AIError("down")

    monkeypatch.setattr(rationale, "complete_with_prompt", failing)
    many = [recommended()] * (rationale.MAX_CONSECUTIVE_FAILURES * 4)
    out = rationale.explain_rows(many, workers=1)
    assert len(calls) == rationale.MAX_CONSECUTIVE_FAILURES
    assert all(r.points_source == rationale.SOURCE_FALLBACK for r in out)


def test_only_convert_to_oar_rows_call_the_model(monkeypatch):
    seen = []

    def spy(prompt_id, **values):
        seen.append(values["material"])
        return Completion(text="", model="gpt-fake", provider="foundry")

    monkeypatch.setattr(rationale, "complete_with_prompt", spy)
    everything = rows()
    out = rationale.explain_rows(everything, workers=2)
    assert len(out) == len(everything)
    assert sorted(seen) == sorted(r.material for r in everything if r.category is Cat.RECOMMENDED_FOR_OAR)


def test_the_prompt_renders_with_the_variables_the_module_supplies():
    """A missing or extra placeholder raises at render time; this is the check that the two agree."""
    from app.core.prompts import get_prompt

    prompt = get_prompt(rationale.PROMPT_ID)
    assert prompt.version == 2
    prompt.render(**rationale._context(recommended()))

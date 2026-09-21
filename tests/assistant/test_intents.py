"""The free-text box.

Classification is pure and is tested here in full. The four answer functions
read real read models and are covered by the Postgres-backed smoke tests; what
matters most about them is covered here anyway, because it is structural rather
than numeric: which questions are declined, and how.
"""

from __future__ import annotations

import pytest

from app.assistant import intents
from app.assistant.intents import Intent, classify
from app.core.config import Settings


class TestClassification:
    @pytest.mark.parametrize(
        "question,expected",
        [
            ("Which repairs are overdue?", Intent.REPAIR),
            ("What is coming back from the vendor?", Intent.REPAIR),
            ("Which OAR materials have no plan?", Intent.OAR),
            ("Do we have this at another plant?", Intent.OAR),
            ("What needs approval?", Intent.APPROVAL),
            ("What is waiting on me?", Intent.APPROVAL),
            ("What is the reorder point?", Intent.STOCK),
            ("Which critical spares are at risk?", Intent.STOCK),
        ],
    )
    def test_the_documented_questions_are_recognised(self, question, expected) -> None:
        assert classify(question) is expected

    def test_matching_is_case_insensitive(self) -> None:
        assert classify("WHICH REPAIRS ARE OVERDUE?") is Intent.REPAIR

    def test_an_unrelated_question_matches_nothing(self) -> None:
        assert classify("What is the weather in Cape Town?") is Intent.NONE

    def test_empty_input_matches_nothing(self) -> None:
        assert classify("") is Intent.NONE
        assert classify(None) is Intent.NONE

    def test_approvals_beat_repairs_when_both_appear(self) -> None:
        """"Waiting for approval on an overdue repair" is an approvals
        question. Order in the bucket list is what decides it, so it is pinned
        rather than left to dictionary ordering nobody checks."""
        assert classify("pending approval on an overdue repair") is Intent.APPROVAL

    def test_rop_is_padded_so_it_cannot_match_inside_a_word(self) -> None:
        """" rop " rather than "rop", or "europe" becomes an inventory
        question."""
        assert classify("our europe supplier") is Intent.NONE
        assert classify("what is the rop for this") is Intent.STOCK


class TestTheDeclinedIntent:
    """Recognised, then refused, because Initiative 07 is not built here."""

    def test_stock_questions_are_declined_rather_than_answered(self) -> None:
        answer = intents._stock_declined()
        assert answer.intent is Intent.STOCK
        assert answer.answered is False

    def test_it_says_why_rather_than_just_failing(self) -> None:
        answer = intents._stock_declined()
        assert "Initiative 07 is not built" in answer.text

    def test_it_refuses_to_answer_from_the_wrong_module(self) -> None:
        """A reorder-point answer assembled out of I08 register rows would be
        wrong in a way nobody could see."""
        answer = intents._stock_declined()
        assert "looks right and is not" in answer.text

    def test_it_offers_what_can_be_asked_instead(self) -> None:
        assert intents._stock_declined().suggestions == intents.ANSWERABLE


class TestUnmatchedQuestions:
    def test_the_refusal_lists_what_is_answerable(self) -> None:
        """A box that silently does nothing teaches people it is broken; one
        that guesses teaches them it is unreliable."""
        answer = intents._unmatched()
        assert answer.answered is False
        for question in intents.ANSWERABLE:
            assert question in answer.text

    def test_the_scope_boundary_is_stated_on_every_answer(self) -> None:
        """General Q&A is a separate scope item and must not be absorbed
        silently into WS7."""
        assert "has not been scoped or agreed" in intents._unmatched().note


class TestTheFeatureFlag:
    def test_it_can_be_switched_off_entirely(self) -> None:
        answer = intents.answer(
            db=None,  # never reached
            text="which repairs are overdue?",
            settings=Settings(assistant_free_text_intents_enabled=False),
        )
        assert answer.answered is False
        assert "switched off" in answer.text


class TestNoModelIsInvolved:
    def test_the_module_does_not_import_the_ai_layer(self) -> None:
        """This path is deterministic by design. An import of the LLM layer here
        would be the first step towards the general-Q&A product that has
        explicitly not been agreed."""
        import pathlib

        source = pathlib.Path(intents.__file__).read_text(encoding="utf-8")
        assert "core.ai" not in source
        assert "core.prompts" not in source

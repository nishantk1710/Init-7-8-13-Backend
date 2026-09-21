"""The two conversations, as arithmetic.

``next_step`` is a pure function of the answers so far, so the whole script can
be tested by handing it answer sets -- no database, no session, no HTTP. That is
the payoff of deriving the step from the turns instead of storing a cursor.

What these tests mostly pin is the **shape** of each conversation: which
question follows which answer, and which questions are skipped when there is
nothing to ask. Wording is deliberately not asserted except where the wording
carries a guarantee (that nothing is blocked, that a session ID is handed back).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.assistant import script
from app.assistant.router import Flow
from app.assistant.script import next_step
from app.assistant.steps import StepKind
from app.initiatives.i13.quantity import QuantitySuggestionConfig, suggest
from tests.assistant.conftest import (
    TODAY,
    i08_assessment,
    i13_assessment,
    repair_line,
    universe_row,
    watch_metric,
)

SESSION = "S7K2M4P8Q1"
CATEGORIES = ["URGENT_BREAKDOWN", "NO_SUITABLE_REPAIRABLE", "OTHER"]
QTY_CONFIG = QuantitySuggestionConfig(
    cover_ceiling_months=Decimal("12"), lookback_months=12, min_history_consumptions=3
)


def i08_step(answers, assessment=None):
    return next_step(
        flow=Flow.I08,
        session_id=SESSION,
        assessment=assessment or i08_assessment(),
        answers=answers,
        today=TODAY,
        reason_categories=CATEGORIES,
    )


def i13_step(answers, assessment=None, suggestion=None):
    return next_step(
        flow=Flow.I13,
        session_id=SESSION,
        assessment=assessment or i13_assessment(),
        answers=answers,
        today=TODAY,
        reason_categories=CATEGORIES,
        suggestion=suggestion,
    )


class TestI08Shape:
    def test_it_opens_by_challenging_the_purchase(self) -> None:
        step = i08_step({})
        assert step.id == script.I08_ASSESSMENT
        assert step.kind is StepKind.CHOICE
        assert {c.value for c in step.choices} == {script.USE_EXISTING, script.PROCEED_NEW}

    def test_the_assessment_card_travels_with_the_question(self) -> None:
        """The numbers are part of the question, not decoration around it."""
        step = i08_step({})
        assert step.facts is not None
        assert step.facts["flow"] == "i08"
        assert step.facts["openRepairLines"] == 1

    def test_caveats_go_in_the_footnote_not_the_prompt(self) -> None:
        """A sentence that hedges every clause is unreadable; a list under it
        is not."""
        step = i08_step({})
        assert step.footnote
        assert "physically reached" in step.footnote

    def test_taking_the_advice_ends_the_conversation(self) -> None:
        step = i08_step({script.I08_ASSESSMENT: {"choice": script.USE_EXISTING}})
        assert step.is_terminal
        assert step.session_id == SESSION

    def test_going_ahead_asks_for_a_justification(self) -> None:
        step = i08_step({script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW}})
        assert step.id == script.I08_JUSTIFICATION
        assert step.kind is StepKind.FORM
        assert {f.name for f in step.fields} == {"reason_category", "free_text"}

    def test_the_justification_ends_the_conversation(self) -> None:
        step = i08_step(
            {
                script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW},
                script.I08_JUSTIFICATION: {
                    "reason_category": "URGENT_BREAKDOWN",
                    "free_text": "Mill 3 down.",
                },
            }
        )
        assert step.is_terminal


class TestI08SkipsTheChallengeWhenThereIsNothingToChallenge:
    def test_no_repairable_unit_goes_straight_to_the_end(self) -> None:
        """A question with only one sensible answer trains people to click
        through the next one."""
        assessment = i08_assessment(rows=[universe_row(stock_on_hand=Decimal("0"))], lines=[])
        step = i08_step({}, assessment)
        assert step.is_terminal
        assert "go ahead" in step.prompt

    def test_stock_on_the_shelf_still_gets_challenged(self) -> None:
        assessment = i08_assessment(rows=[universe_row(stock_on_hand=Decimal("3"))], lines=[])
        step = i08_step({}, assessment)
        assert step.id == script.I08_ASSESSMENT


class TestI13Shape:
    def test_it_opens_by_asking_whether_to_continue(self) -> None:
        step = i13_step({})
        assert step.id == script.I13_ASSESSMENT
        assert {c.value for c in step.choices} == {script.PROCEED, script.NOT_NEEDED}

    def test_declining_ends_it_but_the_advice_is_still_recorded(self) -> None:
        """'Advice given, not acted on' is a number both FRSs want."""
        step = i13_step({script.I13_ASSESSMENT: {"choice": script.NOT_NEEDED}})
        assert step.is_terminal
        assert "advice you were shown is still recorded" in step.prompt

    def test_continuing_asks_for_the_plan(self) -> None:
        step = i13_step({script.I13_ASSESSMENT: {"choice": script.PROCEED}})
        assert step.id == script.I13_CAPTURE_PLAN
        assert step.kind is StepKind.FORM

    def test_the_plan_form_asks_for_a_window_not_a_single_date(self) -> None:
        """FR-2(b). FR-7 breaches on 'window end plus grace', which is a
        different date from the single planned_use_date ACT reads today."""
        step = i13_step({script.I13_ASSESSMENT: {"choice": script.PROCEED}})
        names = {f.name for f in step.fields}
        assert {"window_start", "window_end"} <= names

    def test_the_optional_fields_are_optional(self) -> None:
        """FR-2(b) says cost centre or order 'where known' -- so never required,
        and never inferred."""
        step = i13_step({script.I13_ASSESSMENT: {"choice": script.PROCEED}})
        by_name = {f.name: f for f in step.fields}
        assert by_name["cost_centre"].required is False
        assert by_name["order_number"].required is False
        assert by_name["window_start"].required is False
        assert by_name["purpose"].required is True

    def test_the_planned_quantity_defaults_to_what_they_came_in_with(self) -> None:
        step = i13_step({script.I13_ASSESSMENT: {"choice": script.PROCEED}})
        by_name = {f.name: f for f in step.fields}
        assert by_name["planned_quantity"].default == "5"


class TestI13Quantity:
    """The suggestion is only raised when there is something to say about it."""

    def _answers(self, quantity="5"):
        return {
            script.I13_ASSESSMENT: {"choice": script.PROCEED},
            script.I13_CAPTURE_PLAN: {
                "purpose": "Mill rebuild",
                "planned_quantity": quantity,
                "window_start": None,
                "window_end": None,
                "cost_centre": None,
                "order_number": None,
            },
        }

    def test_no_suggestion_available_ends_the_conversation(self) -> None:
        suggestion = suggest(watch_metric(consumption_count_12m=1), Decimal("5"), QTY_CONFIG)
        step = i13_step(self._answers(), suggestion=suggestion)
        assert step.is_terminal
        assert "No quantity suggestion is offered" in step.prompt

    def test_agreeing_with_the_suggestion_ends_the_conversation(self) -> None:
        """Nothing to challenge, so no question is asked."""
        suggestion = suggest(watch_metric(stock_on_hand=Decimal("0")), Decimal("2"), QTY_CONFIG)
        assert suggestion.is_override is False
        step = i13_step(self._answers("2"), suggestion=suggestion)
        assert step.is_terminal

    def test_wanting_more_than_suggested_raises_the_challenge(self) -> None:
        suggestion = suggest(watch_metric(stock_on_hand=Decimal("10")), Decimal("5"), QTY_CONFIG)
        assert suggestion.is_override is True
        step = i13_step(self._answers(), suggestion=suggestion)
        assert step.id == script.I13_QUANTITY
        assert {c.value for c in step.choices} == {
            script.ACCEPT_SUGGESTED,
            script.KEEP_REQUESTED,
        }

    def test_accepting_the_suggestion_ends_it_with_no_justification(self) -> None:
        suggestion = suggest(watch_metric(stock_on_hand=Decimal("10")), Decimal("5"), QTY_CONFIG)
        answers = self._answers() | {script.I13_QUANTITY: {"choice": script.ACCEPT_SUGGESTED}}
        step = i13_step(answers, suggestion=suggestion)
        assert step.is_terminal

    def test_keeping_the_larger_quantity_asks_why(self) -> None:
        suggestion = suggest(watch_metric(stock_on_hand=Decimal("10")), Decimal("5"), QTY_CONFIG)
        answers = self._answers() | {script.I13_QUANTITY: {"choice": script.KEEP_REQUESTED}}
        step = i13_step(answers, suggestion=suggestion)
        assert step.id == script.I13_QUANTITY_JUSTIFICATION
        assert step.kind is StepKind.FORM

    def test_the_override_justification_ends_it(self) -> None:
        suggestion = suggest(watch_metric(stock_on_hand=Decimal("10")), Decimal("5"), QTY_CONFIG)
        answers = self._answers() | {
            script.I13_QUANTITY: {"choice": script.KEEP_REQUESTED},
            script.I13_QUANTITY_JUSTIFICATION: {
                "reason_category": "OTHER",
                "free_text": "Shutdown coming.",
            },
        }
        step = i13_step(answers, suggestion=suggestion)
        assert step.is_terminal


class TestNothingIsEverBlocked:
    """The platform cannot write to SAP, so the script must not pretend to
    stop anything. Every path reaches a terminal step."""

    @pytest.mark.parametrize(
        "answers",
        [
            {script.I08_ASSESSMENT: {"choice": script.USE_EXISTING}},
            {
                script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW},
                script.I08_JUSTIFICATION: {"reason_category": "OTHER", "free_text": "x"},
            },
        ],
    )
    def test_every_i08_path_reaches_the_end(self, answers) -> None:
        assert i08_step(answers).is_terminal

    def test_going_ahead_is_acknowledged_not_refused(self) -> None:
        step = i08_step({script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW}})
        assert "does not block" in step.prompt


class TestTheSessionIdIsAlwaysHandedBack:
    """It is the only thing the requester has to carry out of the conversation."""

    def test_the_terminal_step_carries_the_id(self) -> None:
        step = i08_step({script.I08_ASSESSMENT: {"choice": script.USE_EXISTING}})
        assert step.session_id == SESSION
        assert SESSION in step.prompt

    def test_it_says_what_to_do_with_it(self) -> None:
        step = i08_step({script.I08_ASSESSMENT: {"choice": script.USE_EXISTING}})
        assert "Type it into the reservation in SAP" in step.prompt


class TestReasonCategoriesAreConfiguration:
    def test_the_options_come_from_the_configured_list(self) -> None:
        """VZI has not supplied a vocabulary, so an enum would need a migration
        on the day they do."""
        step = i08_step({script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW}})
        field = next(f for f in step.fields if f.name == "reason_category")
        assert [o.value for o in field.options] == CATEGORIES

    def test_they_are_labelled_as_placeholders(self) -> None:
        step = i08_step({script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW}})
        field = next(f for f in step.fields if f.name == "reason_category")
        assert "not confirmed" in (field.help_text or "")


class TestAnUnroutableFlowIsARefusal:
    def test_no_script_exists_for_a_material_with_no_flow(self) -> None:
        """A session should never have been minted for it in the first place."""
        with pytest.raises(ValueError, match="no script exists"):
            next_step(
                flow=Flow.NONE,
                session_id=SESSION,
                assessment=i08_assessment(),
                answers={},
                today=TODAY,
                reason_categories=CATEGORIES,
            )

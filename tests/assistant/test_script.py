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


#: "No" to the declaration question -- every I08 path asks it before the end.
SKIP = {script.I08_DECLARE_CHOICE: {"choice": script.SKIP_DECLARATION}}
DECLARATION = {
    "quantity": "1",
    "condition_description": "Bearing seized.",
    "fault_category": "BEARING_FAILURE",
    "recommendation": "REPAIRABLE",
    "attestor_name": "J Miller",
    "evidence_reference": None,
}


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

    def test_the_caveats_are_carried_but_not_repeated_under_the_question(
        self,
    ) -> None:
        """They used to be listed under the question as a footnote, because
        the card did not show them. It does now -- against the number each one
        is about -- so a third copy below the question was the same warning
        for the third time.

        They stay on the record, which is what the trace view reads and what
        makes this a presentation change rather than a loss.
        """
        step = i08_step({})
        assert step.footnote is None
        assert any("physically reached" in c for c in step.facts["caveats"])

    def test_the_question_does_not_repeat_the_headline(self) -> None:
        """The card renders `facts["headline"]`. Putting it in the prompt as
        well printed the same paragraph twice, once above the other."""
        step = i08_step({})
        assert step.prompt == "Do you still want to reserve a new one?"
        assert step.facts["headline"]
        assert step.facts["headline"] not in step.prompt

    def test_taking_the_advice_ends_the_conversation(self) -> None:
        step = i08_step({script.I08_ASSESSMENT: {"choice": script.USE_EXISTING}, **SKIP})
        assert step.is_terminal
        assert step.session_id == SESSION

    def test_going_ahead_asks_for_a_justification(self) -> None:
        step = i08_step({script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW}})
        assert step.id == script.I08_JUSTIFICATION
        assert step.kind is StepKind.FORM
        assert {f.name for f in step.fields} == {"reason_category", "free_text"}
        # And it does NOT repeat the assessment card. The conversation is a
        # transcript: the card from the question above is still on screen.
        assert step.facts is None

    def test_the_justification_says_what_a_reason_does(self) -> None:
        """Section 2.5 (27-Oct-2026): a reason buys nothing, and the desk that
        raises the purchase request never sees it. The frontend's request
        builder shows the same text, so it is pinned word for word."""
        step = i08_step({script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW}})
        assert script.I08_REASON_NOTE == (
            "Your reason is kept on the platform. It doesn't buy anything: the "
            "reservation takes whatever usable stock the plant has, and a new unit "
            "is bought only when MRP or an inventory controller raises a purchase "
            "request. The desk that raises it won't see your reason."
        )
        first, _, rest = step.prompt.partition("\n\n")
        assert rest == script.I08_REASON_NOTE
        # The first line is the request on its own: the request builder quotes
        # it when a reason is missing.
        assert first.startswith("Understood. The platform does not block anything.")
        assert "in stock or on repair" in first
        assert "\n" not in first

    def test_using_the_existing_unit_covers_a_requested_repair_too(self) -> None:
        step = i08_step({})
        [existing] = [c for c in step.choices if c.value == script.USE_EXISTING]
        assert existing.description == "A unit in stock or on repair covers this need."

    def test_the_justification_ends_the_conversation(self) -> None:
        step = i08_step(
            {
                script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW},
                script.I08_JUSTIFICATION: {
                    "reason_category": "URGENT_BREAKDOWN",
                    "free_text": "Mill 3 down.",
                },
                **SKIP,
            }
        )
        assert step.is_terminal


class TestI08SkipsTheChallengeWhenThereIsNothingToChallenge:
    def test_no_repairable_unit_goes_straight_to_the_end(self) -> None:
        """A question with only one sensible answer trains people to click
        through the next one."""
        assessment = i08_assessment(rows=[universe_row(stock_on_hand=Decimal("0"))], lines=[])
        step = i08_step({}, assessment)
        # The only question left is the declaration, and it carries the card.
        assert step.id == script.I08_DECLARE_CHOICE
        assert step.facts is not None
        step = i08_step(SKIP, assessment)
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

    def test_every_plan_field_is_required(self) -> None:
        """Without a window end a plan can never breach, so both dates are
        required alongside the purpose and quantity."""
        step = i13_step({script.I13_ASSESSMENT: {"choice": script.PROCEED}})
        by_name = {f.name: f for f in step.fields}
        for name in ("purpose", "planned_quantity", "window_start", "window_end"):
            assert by_name[name].required is True, name

    def test_the_quantity_asks_how_many_to_procure(self) -> None:
        step = i13_step({script.I13_ASSESSMENT: {"choice": script.PROCEED}})
        by_name = {f.name: f for f in step.fields}
        assert by_name["planned_quantity"].label == "How many should be procured"

    def test_the_plan_form_does_not_ask_for_cost_centre_or_work_order(self) -> None:
        step = i13_step({script.I13_ASSESSMENT: {"choice": script.PROCEED}})
        names = {f.name for f in step.fields}
        assert names.isdisjoint({"cost_centre", "order_number"})

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
            },
        }

    def test_no_suggestion_available_asks_why_this_quantity(self) -> None:
        """Decided 16 Oct 2026: with no history to size against, the quantity
        rests on the requester's word alone, so that word is recorded."""
        suggestion = suggest(watch_metric(consumption_count_12m=1), Decimal("5"), QTY_CONFIG)
        step = i13_step(self._answers(), suggestion=suggestion)
        assert step.id == script.I13_QUANTITY_JUSTIFICATION
        assert {f.name for f in step.fields} == {"reason_category", "free_text"}
        assert "No quantity is suggested" in step.prompt
        assert "why 5 is needed" in step.prompt

    def test_no_suggestion_ends_once_the_reason_is_given(self) -> None:
        suggestion = suggest(watch_metric(consumption_count_12m=1), Decimal("5"), QTY_CONFIG)
        answers = {
            **self._answers(),
            script.I13_QUANTITY_JUSTIFICATION: {"reason_category": "OTHER", "free_text": "New line"},
        }
        step = i13_step(answers, suggestion=suggestion)
        assert step.is_terminal
        assert "Plan recorded at 5, with your reason" in step.prompt

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
            {script.I08_ASSESSMENT: {"choice": script.USE_EXISTING}, **SKIP},
            {
                script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW},
                script.I08_JUSTIFICATION: {"reason_category": "OTHER", "free_text": "x"},
                **SKIP,
            },
            {
                script.I08_ASSESSMENT: {"choice": script.USE_EXISTING},
                script.I08_DECLARE_CHOICE: {"choice": script.DECLARE},
                script.I08_DECLARATION: DECLARATION,
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
        step = i08_step({script.I08_ASSESSMENT: {"choice": script.USE_EXISTING}, **SKIP})
        assert step.session_id == SESSION
        assert SESSION in step.prompt

    def test_it_says_what_to_do_with_it(self) -> None:
        step = i08_step({script.I08_ASSESSMENT: {"choice": script.USE_EXISTING}, **SKIP})
        assert step.prompt.endswith(
            f"Type the session reference {SESSION} into the reservation's item text in SAP."
        )

    def test_a_request_line_names_the_request_reference_once(self) -> None:
        """28-Oct-2026: one reference, one field. A request line's reference is
        the request's, typed once into the header text; the session ID is not
        named at all, so the two cannot be confused."""
        step = next_step(
            flow=Flow.I08,
            session_id=SESSION,
            assessment=i08_assessment(),
            answers={script.I08_ASSESSMENT: {"choice": script.USE_EXISTING}, **SKIP},
            today=TODAY,
            reason_categories=CATEGORIES,
            request_id="R4K7M2P9Q3",
        )
        assert step.is_terminal and step.session_id == SESSION
        assert step.prompt.endswith(
            "Type the request reference R4K7M2P9Q3 once into the reservation's header "
            "text in SAP; SAP copies it to every item."
        )
        assert SESSION not in step.prompt and "item text" not in step.prompt

    def test_the_i13_ending_names_the_request_reference_too(self) -> None:
        step = next_step(
            flow=Flow.I13,
            session_id=SESSION,
            assessment=i13_assessment(),
            answers={script.I13_ASSESSMENT: {"choice": script.NOT_NEEDED}},
            today=TODAY,
            reason_categories=CATEGORIES,
            request_id="R4K7M2P9Q3",
        )
        assert step.is_terminal
        assert "request reference R4K7M2P9Q3 once into the reservation's header text" in step.prompt


class TestNoDoubleDashes:
    """28-Oct-2026: no ``--`` used as a dash in anything the assistant says."""

    @pytest.mark.parametrize(
        "answers",
        [
            {},
            {script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW}},
            {script.I08_ASSESSMENT: {"choice": script.USE_EXISTING}},
            {script.I08_ASSESSMENT: {"choice": script.USE_EXISTING}, **SKIP},
            {
                script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW},
                script.I08_JUSTIFICATION: {"reason_category": "OTHER", "free_text": "x"},
                **SKIP,
            },
        ],
    )
    def test_i08_steps(self, answers) -> None:
        step = i08_step(answers)
        texts = [step.prompt, *(c.label for c in step.choices), *(c.description or "" for c in step.choices)]
        texts += [f.help_text or "" for f in step.fields]
        if step.facts:
            texts += [step.facts["headline"], *step.facts["caveats"]]
        assert not [t for t in texts if "--" in t]

    @pytest.mark.parametrize(
        "answers",
        [{}, {script.I13_ASSESSMENT: {"choice": script.NOT_NEEDED}}],
    )
    def test_i13_steps(self, answers) -> None:
        step = i13_step(answers)
        texts = [step.prompt, *(c.label for c in step.choices), *(c.description or "" for c in step.choices)]
        assert not [t for t in texts if "--" in t]


class TestI08DeclaresTheFaultyUnit:
    """Option B (27-Oct-2026): the declaration is asked after the decision and
    any justification, and in place of the immediate end when no unit exists."""

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
    def test_it_is_asked_after_the_decision(self, answers) -> None:
        step = i08_step(answers)
        assert step.id == script.I08_DECLARE_CHOICE
        assert step.kind is StepKind.CHOICE
        assert step.prompt == "Are you taking out a faulty unit that should go for repair?"
        assert [(c.value, c.label) for c in step.choices] == [
            ("declare", "Yes, declare it now"),
            ("skip", "No"),
        ]
        # The card is a few lines up already.
        assert step.facts is None

    def test_going_ahead_asks_the_reason_first(self) -> None:
        step = i08_step({script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW}})
        assert step.id == script.I08_JUSTIFICATION

    def test_declare_opens_the_form(self) -> None:
        from app.initiatives.i8.config import get_i8_settings

        step = i08_step(
            {
                script.I08_ASSESSMENT: {"choice": script.USE_EXISTING},
                script.I08_DECLARE_CHOICE: {"choice": script.DECLARE},
            }
        )
        assert step.id == script.I08_DECLARATION
        assert step.kind is StepKind.FORM
        fields = {f.name: f for f in step.fields}
        assert list(fields) == [
            "quantity", "condition_description", "fault_category",
            "recommendation", "attestor_name", "evidence_reference",
        ]
        assert fields["quantity"].default == 1
        assert not fields["evidence_reference"].required
        assert fields["attestor_name"].label == "Your name (typed, not a sign-in)"
        assert [o.value for o in fields["fault_category"].options] == list(
            get_i8_settings().fault_category_list
        )
        assert [(o.value, o.label) for o in fields["recommendation"].options] == [
            ("REPAIRABLE", "Repairable"),
            ("BEYOND_ECONOMICAL_REPAIR", "Beyond economical repair"),
            ("SCRAP", "Scrap"),
        ]

    def test_the_end_after_declaring_is_the_same_as_after_skipping(self) -> None:
        decided = {script.I08_ASSESSMENT: {"choice": script.USE_EXISTING}}
        skipped = i08_step({**decided, **SKIP})
        declared = i08_step(
            {
                **decided,
                script.I08_DECLARE_CHOICE: {"choice": script.DECLARE},
                script.I08_DECLARATION: DECLARATION,
            }
        )
        assert skipped.is_terminal and declared.is_terminal
        assert skipped.prompt == declared.prompt


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

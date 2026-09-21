"""Validating an answer against the step it answers.

Pure: ``validate`` takes a step and a payload and returns the normalised answer
or raises. No database.

Why this is worth testing hard rather than treating as form-handling: the turn
table is append-only, so a bad row is permanent -- and these rows are read by a
compliance engine. A malformed planned quantity is not a display bug later, it
is a wrong ``PLAN_BREACH`` finding against a named person.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.assistant import script
from app.assistant.router import Flow
from app.assistant.script import next_step
from app.assistant.turns import AnswerError, validate
from app.core.config import Settings
from tests.assistant.conftest import TODAY, i08_assessment, i13_assessment

SETTINGS = Settings()
CATEGORIES = ["URGENT_BREAKDOWN", "NO_SUITABLE_REPAIRABLE", "OTHER"]


def _i08(answers):
    return next_step(
        flow=Flow.I08,
        session_id="S7K2M4P8Q1",
        assessment=i08_assessment(),
        answers=answers,
        today=TODAY,
        reason_categories=CATEGORIES,
    )


def _i13(answers):
    return next_step(
        flow=Flow.I13,
        session_id="S7K2M4P8Q1",
        assessment=i13_assessment(),
        answers=answers,
        today=TODAY,
        reason_categories=CATEGORIES,
    )


CHOICE_STEP = _i08({})
JUSTIFICATION_STEP = _i08({script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW}})
PLAN_STEP = _i13({script.I13_ASSESSMENT: {"choice": script.PROCEED}})


def _plan(**overrides):
    payload = {
        "purpose": "Mill 3 rebuild",
        "planned_quantity": "2",
        "window_start": "2026-08-01",
        "window_end": "2026-08-31",
        "cost_centre": None,
        "order_number": None,
    }
    payload.update(overrides)
    return payload


class TestChoiceSteps:
    def test_a_valid_choice_is_accepted(self) -> None:
        assert validate(CHOICE_STEP, {"choice": script.PROCEED_NEW}, SETTINGS) == {
            "choice": script.PROCEED_NEW
        }

    def test_an_unoffered_choice_is_refused(self) -> None:
        with pytest.raises(AnswerError, match="choice must be one of"):
            validate(CHOICE_STEP, {"choice": "something_else"}, SETTINGS)

    def test_a_missing_choice_is_refused(self) -> None:
        with pytest.raises(AnswerError, match="choice must be one of"):
            validate(CHOICE_STEP, {}, SETTINGS)


class TestFormSteps:
    def test_a_complete_plan_is_accepted_and_normalised(self) -> None:
        cleaned = validate(PLAN_STEP, _plan(), SETTINGS)
        assert cleaned["planned_quantity"] == "2"
        assert cleaned["window_start"] == "2026-08-01"

    def test_a_required_field_is_required(self) -> None:
        with pytest.raises(AnswerError, match="purpose is required"):
            validate(PLAN_STEP, _plan(purpose=""), SETTINGS)

    def test_optional_fields_may_be_omitted(self) -> None:
        """FR-2(b) says cost centre 'where known'. Blank is a real answer."""
        cleaned = validate(PLAN_STEP, _plan(window_start=None, window_end=None), SETTINGS)
        assert cleaned["window_start"] is None
        assert cleaned["cost_centre"] is None

    def test_an_unknown_field_is_refused_rather_than_ignored(self) -> None:
        """Silently dropping a field the caller believed it was sending is how a
        plan ends up missing its window."""
        with pytest.raises(AnswerError, match="does not take"):
            validate(PLAN_STEP, _plan(sneaky="value"), SETTINGS)


class TestQuantities:
    def test_a_non_numeric_quantity_is_refused(self) -> None:
        with pytest.raises(AnswerError, match="must be a number"):
            validate(PLAN_STEP, _plan(planned_quantity="two"), SETTINGS)

    @pytest.mark.parametrize("bad", ["0", "-1"])
    def test_a_non_positive_quantity_is_refused(self, bad: str) -> None:
        with pytest.raises(AnswerError, match="greater than zero"):
            validate(PLAN_STEP, _plan(planned_quantity=bad), SETTINGS)

    def test_a_decimal_quantity_survives_exactly(self) -> None:
        """Via str, never float. 0.1 from JSON is not 0.1, and this number
        reaches an append-only record a compliance engine reads."""
        cleaned = validate(PLAN_STEP, _plan(planned_quantity="0.1"), SETTINGS)
        assert Decimal(cleaned["planned_quantity"]) == Decimal("0.1")


class TestDatesAndTheWindow:
    def test_a_malformed_date_is_refused(self) -> None:
        with pytest.raises(AnswerError, match="must be a date as YYYY-MM-DD"):
            validate(PLAN_STEP, _plan(window_start="01/08/2026"), SETTINGS)

    def test_a_window_that_ends_before_it_starts_is_refused(self) -> None:
        """Not a typo to preserve: it would make FR-7's 'window end plus grace'
        breach fire immediately, against somebody who meant the opposite."""
        with pytest.raises(AnswerError, match="ends .* before it starts"):
            validate(
                PLAN_STEP,
                _plan(window_start="2026-08-31", window_end="2026-08-01"),
                SETTINGS,
            )

    def test_a_single_day_window_is_fine(self) -> None:
        cleaned = validate(
            PLAN_STEP, _plan(window_start="2026-08-01", window_end="2026-08-01"), SETTINGS
        )
        assert cleaned["window_end"] == "2026-08-01"

    def test_only_one_end_of_the_window_is_allowed(self) -> None:
        """A requester may know when they will start and not when they finish."""
        cleaned = validate(PLAN_STEP, _plan(window_end=None), SETTINGS)
        assert cleaned["window_start"] == "2026-08-01"
        assert cleaned["window_end"] is None


class TestSelectFields:
    def test_a_configured_category_is_accepted(self) -> None:
        cleaned = validate(
            JUSTIFICATION_STEP,
            {"reason_category": "URGENT_BREAKDOWN", "free_text": "Mill down."},
            SETTINGS,
        )
        assert cleaned["reason_category"] == "URGENT_BREAKDOWN"

    def test_it_is_case_insensitive(self) -> None:
        cleaned = validate(
            JUSTIFICATION_STEP,
            {"reason_category": "urgent_breakdown", "free_text": "Mill down."},
            SETTINGS,
        )
        assert cleaned["reason_category"] == "URGENT_BREAKDOWN"

    def test_an_unconfigured_category_is_refused(self) -> None:
        with pytest.raises(AnswerError, match="must be one of"):
            validate(
                JUSTIFICATION_STEP,
                {"reason_category": "INVENTED", "free_text": "x"},
                SETTINGS,
            )

    def test_free_text_is_required(self) -> None:
        """A category alone records that a box was ticked, not that anybody
        thought about it."""
        with pytest.raises(AnswerError, match="free_text is required"):
            validate(
                JUSTIFICATION_STEP,
                {"reason_category": "OTHER", "free_text": "   "},
                SETTINGS,
            )


class TestTerminalSteps:
    def test_a_finished_conversation_cannot_be_answered(self) -> None:
        terminal = _i08({script.I08_ASSESSMENT: {"choice": script.USE_EXISTING}})
        with pytest.raises(AnswerError, match="end of this conversation"):
            validate(terminal, {"choice": "anything"}, SETTINGS)

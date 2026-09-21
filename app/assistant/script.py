"""The two conversations, as pure functions.

State is the answers, not a cursor
-----------------------------------
:func:`next_step` takes everything answered so far and returns what comes next.
It stores nothing and reads nothing, and there is no "current step" field
anywhere.

That is deliberate, and it falls out of the tables being append-only. A cursor
would be a mutable pointer into an immutable log -- a second source of truth
that can disagree with the turns, on a record whose whole value is that it
cannot be rewritten. Deriving the step from the answers means the conversation
and its audit trail are the same object.

It also makes the script testable as arithmetic: hand it a set of answers, get a
step, with no database and no session.

What each flow asks
--------------------
**I08** -- a repair may already be open for this part.

    assessment + "do you still want to buy new?"
      -> no  : done, the advice was taken
      -> yes : why? (FR-7 justification) -> done

**I13** -- this part is planned on demand, so what is the plan?

    assessment + "go ahead with this reservation?"
      -> no  : done, nothing to capture
      -> yes : capture the plan (FR-2(b) / FR-4)
                 -> quantity suggestion (FR-3), if one can be made
                      -> accepted : done
                      -> kept     : why? (justification) -> done

Both end by handing back the session ID, because that is the only thing the
requester has to carry back into SAP.

What the script never does
---------------------------
It never blocks. Every path reaches a terminal step, and no branch refuses to
continue because of an answer. The platform cannot write to SAP, so a requester
can close the window and reserve whatever they like -- pretending otherwise in
the script would be a control that is not there. What the script *can* do is
make sure that when somebody goes ahead anyway, the reason is recorded, which is
what I13's ``NO_PLAN``/``QUANTITY_OVERRIDE`` exceptions read.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

from app.assistant.router import Flow
from app.assistant.steps import Choice, Field, FieldType, Step, StepKind
from app.initiatives.i13.quantity import QuantitySuggestion

# --- Step ids. Stable across wording changes; see Step.id. -----------------

I08_ASSESSMENT = "i08_assessment"
I08_JUSTIFICATION = "i08_justification"
I08_DONE = "i08_done"

I13_ASSESSMENT = "i13_assessment"
I13_CAPTURE_PLAN = "i13_capture_plan"
I13_QUANTITY = "i13_quantity"
I13_QUANTITY_JUSTIFICATION = "i13_quantity_justification"
I13_DONE = "i13_done"

#: The answer values the two challenge steps accept.
USE_EXISTING = "use_existing"
PROCEED_NEW = "proceed_new"
PROCEED = "proceed"
NOT_NEEDED = "not_needed"
ACCEPT_SUGGESTED = "accept_suggested"
KEEP_REQUESTED = "keep_requested"


def _reason_field(categories: Sequence[str], label: str) -> Field:
    """The reason-category picker, built from configuration.

    Both FRSs say "reason category plus free text" and neither lists the
    categories, so they are configuration rather than an enum -- open question
    9. Building the field here means the day VZI supplies a vocabulary is an
    ``.env`` change, not a migration and a redeploy.
    """
    return Field(
        name="reason_category",
        label=label,
        type=FieldType.SELECT,
        required=True,
        options=tuple(
            Choice(value=category, label=category.replace("_", " ").title())
            for category in categories
        ),
        help_text="Placeholder categories -- VZI's own list is not confirmed yet.",
    )


_FREE_TEXT = Field(
    name="free_text",
    label="In your own words",
    type=FieldType.TEXTAREA,
    required=True,
    help_text=(
        "The part a person actually reads when this is reviewed. A category on "
        "its own records that a box was ticked, not that anybody thought about it."
    ),
)


def _terminal(step_id: str, session_id: str, message: str) -> Step:
    """The last step. Always hands back the session ID.

    The ID is the only thing the requester has to carry out of this
    conversation, so the instruction is explicit rather than implied by a field
    appearing on screen.
    """
    return Step(
        id=step_id,
        kind=StepKind.TERMINAL,
        prompt=(
            f"{message}\n\nYour session reference is {session_id}. Type it into "
            "the reservation in SAP so this advice can be linked to what you "
            "actually reserve."
        ),
        session_id=session_id,
    )


# --- Initiative 08 ---------------------------------------------------------


def _i08(
    *,
    session_id: str,
    assessment,
    answers: Mapping[str, dict[str, Any]],
    today: date,
    reason_categories: Sequence[str],
) -> Step:
    facts = assessment.as_record(today)
    headline = assessment.headline(today)
    caveats = assessment.verdict.caveats

    if I08_ASSESSMENT not in answers:
        if not assessment.verdict.exists:
            # Nothing to challenge. Going ahead is the right answer, so asking
            # "are you sure?" would be theatre -- and a question with only one
            # sensible answer trains people to click through the next one.
            return _terminal(
                I08_DONE,
                session_id,
                f"{headline} Nothing here suggests holding off, so go ahead.",
            )

        return Step(
            id=I08_ASSESSMENT,
            kind=StepKind.CHOICE,
            prompt=f"{headline}\n\nDo you still want to reserve a new one?",
            choices=(
                Choice(
                    value=USE_EXISTING,
                    label="No -- I will use the existing unit",
                    description="The repaired or in-stock unit covers this need.",
                ),
                Choice(
                    value=PROCEED_NEW,
                    label="Yes -- I still need a new one",
                    description="You will be asked why, and the reason is recorded.",
                ),
            ),
            facts=facts,
            footnote="\n".join(caveats) or None,
        )

    if answers[I08_ASSESSMENT].get("choice") == USE_EXISTING:
        return _terminal(
            I08_DONE,
            session_id,
            "Recorded -- you are using the unit that already exists rather than "
            "buying another. That decision is what this platform exists to "
            "capture.",
        )

    # They are going ahead anyway. FR-7: reason category plus free text.
    if I08_JUSTIFICATION not in answers:
        return Step(
            id=I08_JUSTIFICATION,
            kind=StepKind.FORM,
            prompt=(
                "Understood -- the platform does not block anything. Please "
                "record why a new unit is needed despite the one already "
                "available."
            ),
            fields=(
                _reason_field(reason_categories, "Why a new unit is needed"),
                _FREE_TEXT,
            ),
            facts=facts,
        )

    return _terminal(
        I08_DONE,
        session_id,
        "Recorded, with your reason. Nothing is blocked -- the reservation is "
        "yours to make.",
    )


# --- Initiative 13 ---------------------------------------------------------


def _i13(
    *,
    session_id: str,
    assessment,
    answers: Mapping[str, dict[str, Any]],
    today: date,
    reason_categories: Sequence[str],
    suggestion: QuantitySuggestion | None,
) -> Step:
    facts = assessment.as_record(today)

    if I13_ASSESSMENT not in answers:
        return Step(
            id=I13_ASSESSMENT,
            kind=StepKind.CHOICE,
            prompt=(
                f"{assessment.headline}\n\nThis part is planned on demand, so a "
                "consumption plan is expected. Do you want to go ahead with "
                "this reservation?"
            ),
            choices=(
                Choice(
                    value=PROCEED,
                    label="Yes -- continue",
                    description="You will be asked what the material is for.",
                ),
                Choice(
                    value=NOT_NEEDED,
                    label="No -- I do not need it after all",
                    description="Nothing is captured beyond the advice you were shown.",
                ),
            ),
            facts=facts,
            footnote="\n".join(assessment.caveats) or None,
        )

    if answers[I13_ASSESSMENT].get("choice") == NOT_NEEDED:
        return _terminal(
            I13_DONE,
            session_id,
            "Nothing captured -- you decided against the reservation. The advice "
            "you were shown is still recorded, which is how "
            "'advice given, not acted on' gets counted.",
        )

    # FR-2(b) and FR-4: the consumption plan itself.
    if I13_CAPTURE_PLAN not in answers:
        return Step(
            id=I13_CAPTURE_PLAN,
            kind=StepKind.FORM,
            prompt="What is this material for?",
            fields=(
                Field(
                    name="purpose",
                    label="What it is for",
                    type=FieldType.TEXTAREA,
                    required=True,
                    help_text="The job, machine or work order this is going to.",
                ),
                Field(
                    name="planned_quantity",
                    label="How many you plan to use",
                    type=FieldType.NUMBER,
                    required=True,
                    default=(
                        str(assessment.requested_quantity)
                        if assessment.requested_quantity is not None
                        else None
                    ),
                ),
                # A WINDOW, not a single date -- FR-2(b). FR-7 breaches on
                # "window end plus grace", which is a different date from the
                # single planned_use_date today's detection reads.
                Field(
                    name="window_start",
                    label="Expected to be used from",
                    type=FieldType.DATE,
                    required=False,
                    help_text="Leave blank if you genuinely do not know yet.",
                ),
                Field(
                    name="window_end",
                    label="...and by",
                    type=FieldType.DATE,
                    required=False,
                ),
                # "Where known" -- never inferred, never required.
                Field(
                    name="cost_centre",
                    label="Cost centre",
                    type=FieldType.TEXT,
                    required=False,
                    help_text="Only if you know it.",
                ),
                Field(
                    name="order_number",
                    label="Work order",
                    type=FieldType.TEXT,
                    required=False,
                ),
            ),
            facts=facts,
        )

    # FR-3. The suggestion is computed against the quantity they just planned,
    # not the one the pop-up opened with: the plan is the fresher statement of
    # intent, and it is the number the plan record will carry.
    if suggestion is None or not suggestion.available:
        return _terminal(
            I13_DONE,
            session_id,
            "Plan recorded. No quantity suggestion is offered for this part -- "
            + (
                suggestion.reason
                if suggestion is not None
                else "there is not enough consumption history to base one on."
            ),
        )

    if not suggestion.is_override:
        return _terminal(
            I13_DONE,
            session_id,
            f"Plan recorded. {suggestion.reason}",
        )

    if I13_QUANTITY not in answers:
        return Step(
            id=I13_QUANTITY,
            kind=StepKind.CHOICE,
            prompt=(
                f"{suggestion.reason}\n\nYou planned "
                f"{suggestion.requested_quantity}. Do you want to take the "
                f"suggested {suggestion.suggested_quantity} instead?"
            ),
            choices=(
                Choice(
                    value=ACCEPT_SUGGESTED,
                    label=f"Take {suggestion.suggested_quantity}",
                    description="Keeps cover at or under the ceiling.",
                ),
                Choice(
                    value=KEEP_REQUESTED,
                    label=f"Keep {suggestion.requested_quantity}",
                    description="You will be asked why, and the reason is recorded.",
                ),
            ),
            facts=facts,
        )

    if answers[I13_QUANTITY].get("choice") == ACCEPT_SUGGESTED:
        return _terminal(
            I13_DONE,
            session_id,
            f"Plan recorded at the suggested {suggestion.suggested_quantity}.",
        )

    if I13_QUANTITY_JUSTIFICATION not in answers:
        return Step(
            id=I13_QUANTITY_JUSTIFICATION,
            kind=StepKind.FORM,
            prompt=(
                f"Please record why {suggestion.requested_quantity} is needed "
                f"rather than {suggestion.suggested_quantity}."
            ),
            fields=(
                _reason_field(reason_categories, "Why the larger quantity"),
                _FREE_TEXT,
            ),
            facts=facts,
        )

    return _terminal(
        I13_DONE,
        session_id,
        f"Plan recorded at {suggestion.requested_quantity}, with your reason.",
    )


def next_step(
    *,
    flow: Flow,
    session_id: str,
    assessment,
    answers: Mapping[str, dict[str, Any]],
    today: date,
    reason_categories: Sequence[str],
    suggestion: QuantitySuggestion | None = None,
) -> Step:
    """The next thing to say, given everything answered so far.

    ``answers`` is keyed by step id. Keying by id rather than by position means
    a script that gains a step in the middle does not misread conversations that
    were recorded before it existed -- which matters, because the turns those
    conversations produced cannot be rewritten.
    """
    if flow is Flow.I08:
        return _i08(
            session_id=session_id,
            assessment=assessment,
            answers=answers,
            today=today,
            reason_categories=reason_categories,
        )
    if flow is Flow.I13:
        return _i13(
            session_id=session_id,
            assessment=assessment,
            answers=answers,
            today=today,
            reason_categories=reason_categories,
            suggestion=suggestion,
        )
    raise ValueError(
        f"no script exists for flow {flow!r} -- a session should never have been "
        "minted for it"
    )

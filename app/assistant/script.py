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
      -> no  : (declare?) -> done, the advice was taken
      -> yes : why? (FR-7 justification) -> (declare?) -> done
    no unit at all : (declare?) -> done

    (declare?) = "are you taking out a faulty unit that should go for
    repair?" -> yes : the FR-4 declaration form -> done; no -> done.

**I13** -- this part is planned on demand, so what is the plan?

    assessment + "go ahead with this reservation?"
      -> no  : done, nothing to capture
      -> yes : capture the plan (FR-2(b) / FR-4)
                 -> quantity suggestion (FR-3), if one can be made
                      -> accepted : done
                      -> kept     : why? (justification) -> done
                 -> no suggestion can be made : why this quantity? -> done

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
from app.initiatives.i8.config import get_i8_settings
from app.initiatives.i13.quantity import QuantitySuggestion

# --- Step ids. Stable across wording changes; see Step.id. -----------------

I08_ASSESSMENT = "i08_assessment"
I08_JUSTIFICATION = "i08_justification"
I08_DECLARE_CHOICE = "i08_declare_choice"
I08_DECLARATION = "i08_declaration"
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
DECLARE = "declare"
SKIP_DECLARATION = "skip"

#: The three assessments, as the declaration form labels them. Values are
#: ``attestation.Recommendation``'s; the labels match the register's
#: ``DeclarationCondition`` so the same unit reads the same on both screens.
RECOMMENDATION_LABELS: tuple[tuple[str, str], ...] = (
    ("REPAIRABLE", "Repairable"),
    ("BEYOND_ECONOMICAL_REPAIR", "Beyond economical repair"),
    ("SCRAP", "Scrap"),
)

#: What recording "I want a new one" does, said under the I08 justification
#: prompt. The frontend shows the same text under its request builder
#: (FE ``i08-facts-format.ts``); change both together. A reason recorded here
#: never reaches SAP: the reservation takes whatever usable stock the plant
#: has, and a new unit is bought only when MRP or an inventory controller
#: raises a purchase request -- people who never see the reason.
I08_REASON_NOTE = (
    "Your reason is kept on the platform. It doesn't buy anything: the "
    "reservation takes whatever usable stock the plant has, and a new unit is "
    "bought only when MRP or an inventory controller raises a purchase request. "
    "The desk that raises it won't see your reason."
)


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
        help_text="Placeholder categories. VZI's own list is not confirmed yet.",
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


def reference_instruction(session_id: str, request_id: str | None = None) -> str:
    """The one sentence that says which reference goes where in SAP.

    A request is one SAP reservation: its reference is typed once into the
    header text and SAP copies it to every item. A single session's reference
    goes into the item text. One reference per conversation, named once
    (28-Oct-2026): the closing message used to name the session reference and
    the item text even on a request line, which the session page contradicted.
    """
    if request_id:
        return (
            f"Type the request reference {request_id} once into the reservation's "
            "header text in SAP; SAP copies it to every item."
        )
    return (
        f"Type the session reference {session_id} into the reservation's item "
        "text in SAP."
    )


def _terminal(
    step_id: str, session_id: str, message: str, request_id: str | None = None
) -> Step:
    """The last step. Always hands back the session ID.

    The reference is the only thing the requester has to carry out of this
    conversation, so the instruction is explicit rather than implied by a field
    appearing on screen. ``request_id`` is set on a line of a multi-material
    request, whose reference is the request's, not the session's.
    """
    return Step(
        id=step_id,
        kind=StepKind.TERMINAL,
        prompt=f"{message}\n\n{reference_instruction(session_id, request_id)}",
        session_id=session_id,
    )


# --- Initiative 08 ---------------------------------------------------------


def fault_category_choices() -> tuple[Choice, ...]:
    """The configured fault categories, labelled as the request builder shows them."""
    return tuple(
        Choice(value=category, label=category.replace("_", " ").title())
        for category in get_i8_settings().fault_category_list
    )


def recommendation_choices() -> tuple[Choice, ...]:
    return tuple(Choice(value=value, label=label) for value, label in RECOMMENDATION_LABELS)


def _declaration_or_end(
    *,
    session_id: str,
    answers: Mapping[str, dict[str, Any]],
    ending: str,
    facts: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> Step:
    """Option B (27-Oct-2026): declare the faulty unit in the same visit.

    The person taking a unit out of the machine is the one standing in front
    of it, and this is the one moment the platform reliably has them. Asked on
    every I08 path -- whether or not a unit already exists says nothing about
    whether the one coming off is repairable. ``ending`` is the terminal text
    the path would have given without the question, so skipping changes nothing.
    """
    if I08_DECLARE_CHOICE not in answers:
        return Step(
            id=I08_DECLARE_CHOICE,
            kind=StepKind.CHOICE,
            prompt="Are you taking out a faulty unit that should go for repair?",
            choices=(
                Choice(
                    value=DECLARE,
                    label="Yes, declare it now",
                    description="Record its condition, so the repair can be checked against it.",
                ),
                Choice(value=SKIP_DECLARATION, label="No"),
            ),
            # Only on the first step of a conversation with nothing to
            # challenge: elsewhere the card is already a few lines up.
            facts=facts,
        )
    if answers[I08_DECLARE_CHOICE].get("choice") == DECLARE and I08_DECLARATION not in answers:
        return Step(
            id=I08_DECLARATION,
            kind=StepKind.FORM,
            prompt="Describe the faulty unit you are taking out.",
            fields=(
                Field(name="quantity", label="Quantity", type=FieldType.NUMBER, default=1),
                Field(
                    name="condition_description",
                    label="Condition",
                    type=FieldType.TEXTAREA,
                    help_text="What is wrong with it, as you see it.",
                ),
                Field(
                    name="fault_category",
                    label="Fault",
                    type=FieldType.SELECT,
                    options=fault_category_choices(),
                ),
                Field(
                    name="recommendation",
                    label="Assessment",
                    type=FieldType.SELECT,
                    options=recommendation_choices(),
                ),
                Field(
                    name="attestor_name",
                    label="Your name (typed, not a sign-in)",
                    type=FieldType.TEXT,
                ),
                Field(
                    name="evidence_reference",
                    label="Evidence reference",
                    type=FieldType.TEXT,
                    required=False,
                    help_text="A photo or report reference, if there is one.",
                ),
            ),
        )
    return _terminal(I08_DONE, session_id, ending, request_id)


def _i08(
    *,
    session_id: str,
    assessment,
    answers: Mapping[str, dict[str, Any]],
    today: date,
    reason_categories: Sequence[str],
    request_id: str | None = None,
) -> Step:
    facts = assessment.as_record(today)
    headline = assessment.headline(today)

    if I08_ASSESSMENT not in answers:
        if not assessment.verdict.exists:
            # Nothing to challenge. Going ahead is the right answer, so asking
            # "are you sure?" would be theatre -- and a question with only one
            # sensible answer trains people to click through the next one.
            #
            # The declaration question is still asked, and as the first step
            # it carries the card the terminal used to sit under.
            return _declaration_or_end(
                session_id=session_id,
                answers=answers,
                ending=f"{headline} Nothing here suggests holding off, so go ahead.",
                facts=facts,
                request_id=request_id,
            )

        return Step(
            id=I08_ASSESSMENT,
            kind=StepKind.CHOICE,
            # The question ONLY. `facts` carries the headline and the card
            # renders it, so repeating it here printed the same paragraph
            # twice: once in the card, once in the bubble beneath it.
            prompt="Do you still want to reserve a new one?",
            choices=(
                Choice(
                    value=USE_EXISTING,
                    label="No, I will use the existing unit",
                    # "On repair" covers a requested repair with no PO yet
                    # (27-Oct-2026): "repaired" claimed a unit already fixed.
                    description="A unit in stock or on repair covers this need.",
                ),
                Choice(
                    value=PROCEED_NEW,
                    label="Yes, I still need a new one",
                    description="You will be asked why, and the reason is recorded.",
                ),
            ),
            facts=facts,
            # No footnote. The caveats were listed here, under the question,
            # because the card did not show them. It does now, and against
            # the number each one is about rather than as a block below: an
            # unreliable due date reads "no longer a forecast" on the date
            # itself, an overdue line carries its own warning, a line with no
            # 541 says so, unknown stock says "no stock record", and a vendor
            # with no master record shows its code.
            #
            # They stay in `facts["caveats"]`, so the stored record and the
            # trace view keep every one of them.
        )

    if answers[I08_ASSESSMENT].get("choice") == USE_EXISTING:
        return _declaration_or_end(
            session_id=session_id,
            answers=answers,
            ending="Recorded: you are using the unit that already exists rather than "
            "buying another. That decision is what this platform exists to "
            "capture.",
            request_id=request_id,
        )

    # They are going ahead anyway. FR-7: reason category plus free text.
    if I08_JUSTIFICATION not in answers:
        return Step(
            id=I08_JUSTIFICATION,
            kind=StepKind.FORM,
            # The first line stays the request: the request builder quotes
            # it alone when a reason is missing. The note under it says
            # what the reason does and does not do (27-Oct-2026).
            prompt=(
                "Understood. The platform does not block anything. Please "
                "record why a new unit is needed despite the one in stock or "
                "on repair.\n\n" + I08_REASON_NOTE
            ),
            fields=(
                _reason_field(reason_categories, "Why a new unit is needed"),
                _FREE_TEXT,
            ),
            # No card here. It was repeated so the requester could see what
            # they were overriding, which made sense for one screen at a time
            # -- but the conversation is a transcript, and the card from the
            # question above is still visible a few lines up.
        )

    return _declaration_or_end(
        session_id=session_id,
        answers=answers,
        ending=(
            "Recorded, with your reason. Nothing is blocked: the reservation is "
            "yours to make."
        ),
        request_id=request_id,
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
    request_id: str | None = None,
) -> Step:
    facts = assessment.as_record(today)

    def done(message: str) -> Step:
        return _terminal(I13_DONE, session_id, message, request_id)

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
                    label="Yes, continue",
                    description="You will be asked what the material is for.",
                ),
                Choice(
                    value=NOT_NEEDED,
                    label="No, I do not need it after all",
                    description="Nothing is captured beyond the advice you were shown.",
                ),
            ),
            facts=facts,
            footnote="\n".join(assessment.caveats) or None,
        )

    if answers[I13_ASSESSMENT].get("choice") == NOT_NEEDED:
        return done(
            "Nothing captured: you decided against the reservation. The advice "
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
                    label="How many should be procured",
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
                # single planned_use_date today's detection reads. Both ends
                # are required: a plan without a window end can never breach,
                # so an optional window left most plans unmeasurable.
                Field(
                    name="window_start",
                    label="Expected to be used from",
                    type=FieldType.DATE,
                    required=True,
                ),
                Field(
                    name="window_end",
                    label="...and by",
                    type=FieldType.DATE,
                    required=True,
                ),
                # Cost centre and work order are no longer asked for. The plan
                # columns stay (nullable) so earlier captures keep their values;
                # new plans store None.
            ),
            facts=facts,
        )

    # FR-3. The suggestion is computed against the quantity they just planned,
    # not the one the pop-up opened with: the plan is the fresher statement of
    # intent, and it is the number the plan record will carry.
    if suggestion is None or not suggestion.available:
        # No history to size against, so nothing supports the quantity but the
        # requester's word -- which is therefore asked for (decided 16 Oct
        # 2026). Recorded as QUANTITY_OVERRIDE, the reason-for-this-quantity
        # kind; with no suggestion there is no override exception for it to
        # answer, so it is evidence only.
        planned = suggestion.requested_quantity if suggestion is not None else None
        if I13_QUANTITY_JUSTIFICATION not in answers:
            return Step(
                id=I13_QUANTITY_JUSTIFICATION,
                kind=StepKind.FORM,
                prompt=(
                    (suggestion.reason if suggestion is not None
                     else "No quantity can be suggested for this part.")
                    + "\n\nPlease record why "
                    + (f"{planned} is" if planned is not None else "this quantity is")
                    + " needed."
                ),
                fields=(
                    _reason_field(reason_categories, "Why this quantity"),
                    _FREE_TEXT,
                ),
                facts=facts,
            )
        return done(
            "Plan recorded"
            + (f" at {planned}" if planned is not None else "")
            + ", with your reason. No quantity suggestion could be made for this part.",
        )

    if not suggestion.is_override:
        return done(
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
        return done(
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

    return done(
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
    request_id: str | None = None,
) -> Step:
    """The next thing to say, given everything answered so far.

    ``request_id`` is the multi-material request the session is a line of, if
    any: the closing message then names the request reference, not the
    session's.

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
            request_id=request_id,
        )
    if flow is Flow.I13:
        return _i13(
            session_id=session_id,
            assessment=assessment,
            answers=answers,
            today=today,
            reason_categories=reason_categories,
            suggestion=suggestion,
            request_id=request_id,
        )
    raise ValueError(
        f"no script exists for flow {flow!r} -- a session should never have been "
        "minted for it"
    )

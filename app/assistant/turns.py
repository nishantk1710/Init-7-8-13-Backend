"""Answering the current step, and everything that follows from the answer.

What one call does
-------------------
1. Loads the session and the conversation so far.
2. Works out what the current step is -- by re-deriving it, never by reading a
   stored cursor.
3. Validates the answer against *that* step.
4. Records the turn.
5. Writes whatever the answer commits the requester to -- a consumption plan, a
   quantity decision, a justification.
6. Re-derives the next step and returns it.

Step 2 is what keeps the audit trail and the conversation in agreement. The
script is a pure function of the answers (``app.assistant.script``), so "what
were they being asked?" is always recomputed from the same immutable log the
question was recorded in. There is no state that can rot.

Validation is not paperwork here
---------------------------------
An answer that does not fit its step is refused rather than stored. The turn
table is append-only, so a bad row is permanent -- and unlike most bad rows,
these are read by a compliance engine. ``i13_act`` raises exceptions from what
the requester committed to, so a malformed plan quantity is not a display bug
later, it is a wrong finding against a named person.

When the side effects are written
----------------------------------
Each record is written **once, at the moment its value becomes final**, because
none of them can be updated afterwards:

* the **consumption plan** when the plan form is answered -- that is when the
  requester states it;
* the **quantity suggestion** when the accepted quantity is known, which is
  either immediately (no override to resolve) or after the challenge is
  answered. Writing it at plan capture would mean writing an
  ``accepted_quantity`` that a later step could contradict, into a row that
  cannot be corrected;
* a **justification** when its form is answered.

The plan and the reservation quantity are allowed to differ
------------------------------------------------------------
If a requester plans to use 5 and then accepts a suggestion of 2, the plan still
says 5 and the quantity record says 2. That is not an inconsistency to reconcile
-- it is exactly the acquired-versus-plan variance I13's WATCH mart already
measures, and flattening the two would destroy the signal. Amending a plan would
mean a superseding row; nothing captures that yet and nothing pretends to.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from sqlalchemy.orm import Session as DbSession

from app.assistant import script, session as session_module
from app.assistant.models import (
    AssistantSession,
    AssistantTurn,
    ConsumptionPlanRecord,
    Justification,
    QuantitySuggestionRecord,
)
from app.assistant.router import Flow
from app.assistant.script import next_step
from app.assistant.session import SessionError
from app.assistant.steps import FieldType, Step, StepKind
from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class AnswerError(ValueError):
    """The submitted answer does not fit the step it answers."""


def _decimal(raw: Any, label: str) -> Decimal:
    """A quantity, or a refusal that names the field.

    Via ``str`` so a JSON float never reaches a Decimal directly -- 0.1 from
    JSON is not 0.1, and this number ends up in an append-only record that a
    compliance engine reads.
    """
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError, TypeError) as error:
        raise AnswerError(f"{label} must be a number, got {raw!r}") from error
    if value <= 0:
        raise AnswerError(f"{label} must be greater than zero, got {value}")
    return value


def _date(raw: Any, label: str) -> date | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return date.fromisoformat(str(raw).strip())
    except ValueError as error:
        raise AnswerError(f"{label} must be a date as YYYY-MM-DD, got {raw!r}") from error


def _text(raw: Any) -> str | None:
    if raw is None:
        return None
    cleaned = str(raw).strip()
    return cleaned or None


def validate(step: Step, answer: Mapping[str, Any], settings: Settings) -> dict[str, Any]:
    """Check an answer against its step and return it normalised.

    Separate from recording it so the same rules can be unit-tested without a
    database, and so the API layer turns one exception type into one status code
    rather than guessing at a dozen -- the same split ``attestation.validate``
    uses.
    """
    if step.kind is StepKind.TERMINAL:
        raise AnswerError(
            f"{step.id} is the end of this conversation -- there is nothing to answer."
        )

    if step.kind is StepKind.CHOICE:
        choice = _text(answer.get("choice"))
        allowed = {option.value for option in step.choices}
        if choice not in allowed:
            raise AnswerError(
                f"choice must be one of {sorted(allowed)}, got {answer.get('choice')!r}"
            )
        return {"choice": choice}

    if step.kind is StepKind.MESSAGE:
        return {}

    # FORM. Each field is validated by its declared type, and an unrecognised
    # key is refused rather than ignored: silently dropping a field the caller
    # believed it was sending is how a plan ends up missing its window.
    declared = {field.name for field in step.fields}
    unknown = sorted(set(answer) - declared)
    if unknown:
        raise AnswerError(
            f"{step.id} does not take {unknown}. It takes {sorted(declared)}."
        )

    cleaned: dict[str, Any] = {}
    for field in step.fields:
        raw = answer.get(field.name)

        if raw is None or str(raw).strip() == "":
            if field.required:
                raise AnswerError(f"{field.name} is required")
            cleaned[field.name] = None
            continue

        if field.type is FieldType.NUMBER:
            cleaned[field.name] = str(_decimal(raw, field.name))
        elif field.type is FieldType.DATE:
            parsed = _date(raw, field.name)
            cleaned[field.name] = parsed.isoformat() if parsed else None
        elif field.type is FieldType.SELECT:
            allowed = {option.value for option in field.options}
            value = str(raw).strip().upper()
            if value not in allowed:
                raise AnswerError(
                    f"{field.name} must be one of {sorted(allowed)}, got {raw!r}"
                )
            cleaned[field.name] = value
        else:
            cleaned[field.name] = str(raw).strip()

    # A window that ends before it starts is not a typo to preserve -- it would
    # make FR-7's "window end plus grace" breach fire immediately, against
    # somebody who meant the opposite.
    start, end = cleaned.get("window_start"), cleaned.get("window_end")
    if start and end and date.fromisoformat(end) < date.fromisoformat(start):
        raise AnswerError(
            f"the planned window ends ({end}) before it starts ({start})"
        )

    return cleaned


def _record_plan(
    db: DbSession,
    session: AssistantSession,
    answer: Mapping[str, Any],
    *,
    actor: str,
) -> ConsumptionPlanRecord:
    """I13 FR-4 -- the first consumption plan this platform has ever written.

    ``reservation_number`` and ``reservation_item`` are left null, and that is
    the normal case rather than a gap: the assistant runs *while* the
    reservation is being created, so it does not exist in SAP yet and has no
    number. That is exactly why the requester types the session ID into SAP --
    the link is made afterwards, which is FR-8 and needs ``Bednr`` exposed on
    ``ReservationItemSet`` (blocker B2).
    """
    plan = ConsumptionPlanRecord(
        session_id=session.id,
        reservation_number=None,
        reservation_item=None,
        material=session.material_id,
        plant=session.plant,
        purpose=answer["purpose"],
        planned_quantity=Decimal(answer["planned_quantity"]),
        window_start=_date(answer.get("window_start"), "window_start"),
        window_end=_date(answer.get("window_end"), "window_end"),
        cost_centre=answer.get("cost_centre"),
        order_number=answer.get("order_number"),
        # "OPEN", not a vocabulary of our own. Detection tests
        # `plan.status != "OPEN"` before it will treat a plan as a live
        # commitment, and the reference CSV uses OPEN/CLOSED. A captured
        # plan written as "ACTIVE" parsed as a plan that had been
        # withdrawn -- found by the step-8 end-to-end test, which is
        # exactly the class of mistake it exists to catch.
        status="OPEN",
        captured_by=actor,
    )
    db.add(plan)
    logger.info(
        "Consumption plan captured for session %s: %s at %s, qty %s",
        session.id,
        plan.material,
        plan.plant,
        plan.planned_quantity,
    )
    return plan


def _record_suggestion(
    db: DbSession,
    session: AssistantSession,
    suggestion,
    accepted_quantity: Decimal,
) -> QuantitySuggestionRecord:
    """I13 FR-3 -- what we suggested and what they kept.

    Written once, when ``accepted_quantity`` becomes final. The whole
    arithmetic basis travels with it, including the three configured values,
    which are our defaults until VZI confirms them -- a suggestion whose basis
    cannot be recovered is one nobody can argue with.
    """
    record = QuantitySuggestionRecord(
        session_id=session.id,
        material=session.material_id,
        plant=session.plant,
        requested_quantity=suggestion.requested_quantity,
        suggested_quantity=suggestion.suggested_quantity,
        accepted_quantity=accepted_quantity,
        suggestion_reason=suggestion.reason,
        stock_on_hand=suggestion.stock_on_hand,
        open_po_quantity=suggestion.open_po_quantity,
        average_monthly_consumption=suggestion.average_monthly_consumption,
        months_of_cover=suggestion.months_of_cover,
        cover_ceiling_months=suggestion.config.cover_ceiling_months,
        lookback_months=suggestion.config.lookback_months,
        min_history_consumptions=suggestion.config.min_history_consumptions,
        consumption_count=suggestion.consumption_count,
    )
    db.add(record)
    return record


def _record_justification(
    db: DbSession,
    session: AssistantSession,
    answer: Mapping[str, Any],
    *,
    kind: str,
    actor: str,
) -> Justification:
    """I08 FR-7 / I13 FR-7 -- why they went ahead anyway.

    ``kind`` is what lines this up with ACT's ``QUANTITY_OVERRIDE`` exception
    type and its ``JUSTIFICATION_ADDED`` event, which is why it is a column
    rather than two separate tables.
    """
    justification = Justification(
        session_id=session.id,
        exception_id=None,
        kind=kind,
        reason_category=answer["reason_category"],
        free_text=answer["free_text"],
        material_id=session.material_id,
        plant=session.plant,
        author=actor,
    )
    db.add(justification)
    logger.info(
        "Justification recorded for session %s: %s / %s",
        session.id,
        kind,
        justification.reason_category,
    )
    return justification


def _planned_quantity(answers: Mapping[str, dict]) -> Decimal | None:
    captured = answers.get(script.I13_CAPTURE_PLAN)
    if not captured or captured.get("planned_quantity") is None:
        return None
    return Decimal(captured["planned_quantity"])


def current_step(
    db: DbSession,
    session: AssistantSession,
    *,
    today: date,
    settings: Settings,
) -> tuple[Step, Any, Any]:
    """Re-derive the step this session is waiting on.

    Returns ``(step, assessment, suggestion)``. The assessment is rebuilt rather
    than read back from the stored JSON, because the script needs the live
    object to compose the *next* question -- while the stored copy remains the
    record of what was actually served, which is the thing that must not move.
    """
    from app.assistant.session import _assessment_for  # local: avoids a cycle
    from app.initiatives.i13.config import get_i13_config

    flow = Flow(session.flow)
    routed = _routed_from(session, flow)
    assessment = _assessment_for(
        db,
        routed,
        requested_quantity=session.requested_quantity,
        today=today,
        settings=settings,
        i13_config=get_i13_config(),
    )

    answers = session_module.answers_of(session_module.turns(db, session.id))

    suggestion = None
    if flow is Flow.I13:
        planned = _planned_quantity(answers)
        if planned is not None:
            suggestion = session_module.suggestion_for(assessment, planned)

    step = next_step(
        flow=flow,
        session_id=session.id,
        assessment=assessment,
        answers=answers,
        today=today,
        reason_categories=settings.assistant_justification_reason_category_list,
        suggestion=suggestion,
    )
    return step, assessment, suggestion


def _routed_from(session: AssistantSession, flow: Flow):
    """Rebuild the routing decision from the stored session.

    The routing is replayed from what was recorded rather than recomputed from
    MARC. A material reclassified since the session was minted must not change
    which script an in-flight conversation is following halfway through.
    """
    from app.assistant.router import RoutedFlow
    from app.shared.material_scope import MaterialScope

    return RoutedFlow(
        flow=flow,
        material_id=session.material_id,
        plant=session.plant,
        eighty_series=session.eighty_series,
        material_scope=MaterialScope(session.material_scope),
        mrp_type=session.mrp_type,
    )


def answer(
    db: DbSession,
    session_id: str,
    payload: Mapping[str, Any],
    *,
    actor: str,
    today: date | None = None,
    settings: Settings | None = None,
) -> tuple[Step, AssistantSession]:
    """Record an answer to the current step and return the next one.

    ``actor`` is passed separately from the payload by every caller. An audit
    record whose author is taken from the body it is auditing is not an audit
    record.
    """
    settings = settings or get_settings()
    today = today or date.today()

    session = session_module.load(db, session_id, settings)
    step, assessment, suggestion = current_step(
        db, session, today=today, settings=settings
    )

    if step.is_terminal:
        raise AnswerError(
            f"Session {session.id} is already finished. Its conversation cannot "
            "be reopened -- start a new session if the decision has changed."
        )

    cleaned = validate(step, payload, settings)

    existing = session_module.turns(db, session.id)
    turn = AssistantTurn(
        session_id=session.id,
        sequence=len(existing),
        step_id=step.id,
        step_kind=step.kind.value,
        # The question AS ASKED. The script will be reworded; what this person
        # was asked will not.
        question=step.prompt,
        answer=json.dumps(cleaned, sort_keys=True),
        actor=actor,
    )
    db.add(turn)

    # --- side effects, each written once, when its value becomes final ------

    if step.id == script.I13_CAPTURE_PLAN:
        _record_plan(db, session, cleaned, actor=actor)

    elif step.id == script.I08_JUSTIFICATION:
        _record_justification(db, session, cleaned, kind="NEW_ACQUISITION", actor=actor)

    elif step.id == script.I13_QUANTITY_JUSTIFICATION:
        _record_justification(
            db, session, cleaned, kind="QUANTITY_OVERRIDE", actor=actor
        )

    elif step.id == script.I13_QUANTITY and suggestion is not None:
        accepted = (
            suggestion.suggested_quantity
            if cleaned["choice"] == script.ACCEPT_SUGGESTED
            else suggestion.requested_quantity
        )
        _record_suggestion(db, session, suggestion, accepted)

    db.commit()

    # Re-derive after the write, so the next step sees the turn just recorded.
    following, _, following_suggestion = current_step(
        db, session, today=today, settings=settings
    )

    # A conversation that ends without ever asking about quantity still made a
    # suggestion, and the evidence of benefit is the suggestion plus what was
    # kept. Recorded here rather than at plan capture, because only now is the
    # accepted quantity final.
    if (
        following.is_terminal
        and following_suggestion is not None
        and step.id == script.I13_CAPTURE_PLAN
    ):
        _record_suggestion(
            db,
            session,
            following_suggestion,
            following_suggestion.requested_quantity,
        )

    # The terminal step is recorded as a turn of its own. That is what makes a
    # session COMPLETED rather than merely quiet -- see session.outcome, which
    # derives the outcome instead of storing it.
    if following.is_terminal:
        db.add(
            AssistantTurn(
                session_id=session.id,
                sequence=len(session_module.turns(db, session.id)),
                step_id=following.id,
                step_kind=following.kind.value,
                question=following.prompt,
                answer=None,
                actor=actor,
            )
        )
        db.commit()

    return following, session

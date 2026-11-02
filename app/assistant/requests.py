"""Multi-material requests: several materials checked in one go, at one plant.

A request is a parent row (:class:`AssistantRequest`, ``R`` + 8 + check
character) and one **ordinary session per material**, carrying ``request_id``
and ``line_no``. The requester quotes each line on its SAP reservation item as
``<request>/<n>``. Nothing about a line is new: its turns, plan, suggestion and
justification are written by :mod:`app.assistant.turns` exactly as the
conversation writes them, so the trace, the session log, WATCH, ACT and the
justification screens read request lines without knowing they are any.

Two calls
---------
:func:`preview` -- as rows are added. Routes and assesses each material and
says what the screen should ask: repairable, OAR (with the quantity suggested
**before** anyone names one, and the window that quantity would cover), or no
session at all. Writes nothing.

:func:`create` -- on submit. Every line's answers in one call, recorded in
**one transaction**: if any line is refused, nothing is written. The tables
are append-only, so a half-written request could never be tidied away.

What submit does, in order
--------------------------
1. Refuse a request that is empty, over the line cap, spans plants, repeats a
   material, has no department (platform), names more than one, or names one
   that is not a code (as the opener does). One request is one plant and one
   department (UAT, 21 Oct): the department is picked once and every line's
   session carries it.
2. Re-route and re-assess every line, now -- not trust the preview. If an OAR
   line's suggestion moved since the requester saw it (a snapshot refresh in
   between), refuse with the fresh figures (:class:`RequestConflict`): a
   reason given against a number nobody was shown is not a reason.
3. Write the request, then mint each line's session and **replay its answers
   through** :func:`app.assistant.turns.answer`, step by step, so validation,
   audit turns and side effects are the conversation's own.
4. Commit once. The WATCH/ACT refresh for the OAR lines (``downstream``) runs
   after that, outside the request -- see :func:`apply_downstream`.

A resubmitted draft (same ``idempotency_key``) returns the request it already
created rather than writing a second one.
"""

from __future__ import annotations

import calendar
import json
import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.assistant import departments, ids, script
from app.assistant import session as session_module
from app.assistant import turns as turn_module
from app.assistant.models import (
    AssistantRequest,
    AssistantSession,
    ConsumptionPlanRecord,
    Justification,
    QuantitySuggestionRecord,
)
from app.assistant.router import Flow, RoutedFlow, route
from app.assistant.session import Origin, SessionError, _assessment_for
from app.assistant.turns import AnswerError
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.initiatives.i13.config import get_i13_config
from app.initiatives.i13.quantity import QuantitySuggestion, build_quantity_config, suggest
from app.initiatives.i8.material_number import normalise
from app.shared.plant_scope import IN_SCOPE_PLANTS, is_in_scope

logger = get_logger(__name__)

#: However the setting is configured, a request never holds more than this.
#: Submit holds one worker and one transaction for every line; at ~0.2-0.4 s a
#: line, 50 is already a long wait.
HARD_MAX_LINES = 50

#: The window suggested for a quantity never runs past the cover ceiling, and
#: never shorter than a month.
_MIN_WINDOW_MONTHS = 1

#: A line's answers reach a terminal step in at most this many turns (I13: the
#: assessment, the plan, "take the suggestion?", the reason). A guard, not a
#: tuning value -- exceeding it means the script and this module disagree.
_MAX_STEPS_PER_LINE = 8


class RequestError(ValueError):
    """The request as a whole cannot be accepted. ``line`` is 1-based when one
    line is the cause."""

    def __init__(self, message: str, *, line: int | None = None) -> None:
        super().__init__(message)
        self.line = line


class RequestConflict(RequestError):
    """A suggestion moved between preview and submit. Nothing was written."""

    def __init__(self, changed: list[dict[str, Any]]) -> None:
        super().__init__(
            "The stock figures for "
            + ", ".join(c["materialId"] for c in changed)
            + " changed since you checked them. Review the new suggestions and submit again."
        )
        self.changed = changed


def max_lines(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    return max(1, min(settings.assistant_request_max_lines, HARD_MAX_LINES))


# --- preview -------------------------------------------------------------------


@dataclass(frozen=True)
class Suggested:
    """The quantity offered before any is named, and what it rests on."""

    quantity: Decimal | None
    """``None``: no suggestion can be made (too little history, no rate)."""
    reason: str
    window_start: date | None = None
    window_end: date | None = None
    window_months: int | None = None


@dataclass(frozen=True)
class PreviewLine:
    material_id: str
    plant: str
    kind: str
    """``i13`` / ``i08`` -- a session will be opened; ``out_of_scope`` /
    ``data_gap`` / ``invalid`` -- none will."""
    reason: str
    facts: dict[str, Any] | None = None
    unit_exists: bool | None = None
    """I08 only: a repaired unit in stock or one on repair. ``False`` means
    there is nothing to decide -- the line needs no answer."""
    suggestion: Suggested | None = None
    also_matched: str | None = None


def suggested_window(quantity: Decimal | None, rate: Decimal | None, today: date) -> tuple[date, date, int] | None:
    """From today until ``quantity`` would be used up at ``rate`` a month,
    between one month and the cover ceiling. ``None`` without a rate."""
    if quantity is None or quantity <= 0 or rate is None or rate <= 0:
        return None
    ceiling = int(build_quantity_config().cover_ceiling_months)
    months = max(_MIN_WINDOW_MONTHS, min(ceiling, math.ceil(quantity / rate)))
    return today, _add_months(today, months), months


def _add_months(day: date, months: int) -> date:
    """Calendar months; 31 Jan + 1 month is the last day of February."""
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def uncapped_suggestion(assessment) -> QuantitySuggestion:
    """FR-3's suggestion before a quantity is named: the raw headroom."""
    return suggest(assessment.metric, Decimal("0"), build_quantity_config(), cap_at_requested=False)


def preview(
    db: DbSession,
    materials: Iterable[tuple[str, str]],
    *,
    today: date | None = None,
    settings: Settings | None = None,
) -> list[PreviewLine]:
    """What each material would ask. Writes nothing."""
    settings = settings or get_settings()
    today = today or date.today()
    i13_config = get_i13_config()
    lines: list[PreviewLine] = []

    for material_id, plant in materials:
        material = normalise(material_id) or (material_id or "").strip()
        plant = (plant or "").strip()
        if not is_in_scope(plant):
            lines.append(PreviewLine(material, plant, "invalid", f"Plant {plant!r} is not {' or '.join(IN_SCOPE_PLANTS)}."))
            continue

        routed = route(db, material, plant)
        if not routed.in_scope:
            lines.append(PreviewLine(routed.material_id, plant, "out_of_scope", routed.reason))
            continue

        try:
            assessment = _assessment_for(
                db, routed, requested_quantity=None, today=today, settings=settings, i13_config=i13_config
            )
        except SessionError as error:
            lines.append(PreviewLine(routed.material_id, plant, "data_gap", str(error)))
            continue

        facts = assessment.as_record(today)
        also = routed.also_matched.value if routed.also_matched else None
        if routed.flow is Flow.I08:
            lines.append(
                PreviewLine(
                    routed.material_id, plant, "i08", routed.reason,
                    facts=facts, unit_exists=assessment.verdict.exists, also_matched=also,
                )
            )
            continue

        offered = uncapped_suggestion(assessment)
        window = suggested_window(
            offered.suggested_quantity, assessment.metric.average_monthly_consumption, today
        )
        lines.append(
            PreviewLine(
                routed.material_id, plant, "i13", routed.reason, facts=facts, also_matched=also,
                suggestion=Suggested(
                    quantity=offered.suggested_quantity,
                    reason=offered.reason,
                    window_start=window[0] if window else None,
                    window_end=window[1] if window else None,
                    window_months=window[2] if window else None,
                ),
            )
        )
    return lines


# --- create ----------------------------------------------------------------------


@dataclass(frozen=True)
class LineAnswers:
    """One material's answers, exactly as the screen collected them."""

    material_id: str
    plant: str
    department: str | None = None
    decision: str | None = None
    """I13: ``proceed`` / ``not_needed``. I08: ``use_existing`` /
    ``proceed_new``; ignored when no unit exists."""
    quantity: str | None = None
    window_start: str | None = None
    window_end: str | None = None
    purpose: str | None = None
    reason_category: str | None = None
    free_text: str | None = None
    seen_suggested_quantity: str | None = None
    """What the screen showed as suggested (I13), so a figure that moved since
    can be caught. ``None``: no suggestion was shown."""
    check_seen: bool = False
    """Whether ``seen_suggested_quantity`` was sent at all."""
    declaration: dict[str, Any] | None = None
    """I08 only: the faulty unit declared on this line (27-Oct-2026), keyed as
    the conversation's ``i08_declaration`` form is. ``None``: nothing declared,
    which answers "No" to the declaration question."""


@dataclass
class CreatedRequest:
    request: AssistantRequest
    sessions: list[AssistantSession] = field(default_factory=list)
    replayed: bool = False
    """True when the idempotency key matched an existing request."""
    downstream_keys: list[tuple[str, str, str]] = field(default_factory=list)
    """(material, plant, session) for every OAR line that captured a plan --
    what :func:`apply_downstream` refreshes. Empty on a replay: the first
    submit already scheduled it."""


@dataclass(frozen=True)
class _Prepared:
    line_no: int
    """The session's ``/n``: its place among the lines that open a session."""
    submitted: int
    """Its place in what was submitted -- the number every error names, since
    that is the row the requester is looking at."""
    answers: LineAnswers
    routed: RoutedFlow
    assessment: Any


def _same_quantity(seen: str | None, fresh: Decimal | None) -> bool:
    if seen is None or str(seen).strip() == "":
        return fresh is None
    if fresh is None:
        return False
    try:
        return Decimal(str(seen).strip()) == fresh
    except InvalidOperation:
        return False


def _validate_shape(lines: list[LineAnswers], plant: str, settings: Settings) -> None:
    cap = max_lines(settings)
    if not lines:
        raise RequestError("A request needs at least one material.")
    if len(lines) > cap:
        raise RequestError(f"A request holds up to {cap} materials; this one has {len(lines)}.")
    if not is_in_scope(plant):
        raise RequestError(f"Plant {plant!r} is not {' or '.join(IN_SCOPE_PLANTS)}.")
    seen: dict[str, int] = {}
    for number, line in enumerate(lines, start=1):
        if (line.plant or "").strip() != plant:
            raise RequestError(
                f"{line.material_id} is at plant {line.plant}; this request is for plant {plant}. "
                "One request covers one plant.",
                line=number,
            )
        key = normalise(line.material_id) or line.material_id.strip()
        if key in seen:
            raise RequestError(
                f"{key} is on this request twice (lines {seen[key]} and {number}). "
                "Put the whole quantity on one line.",
                line=number,
            )
        seen[key] = number


def request_department(
    lines: list[LineAnswers], department: str | None, *, origin: Origin = Origin.PLATFORM
) -> str | None:
    """The request's one department: the request-level value, or the one value
    its lines agree on. Refuses two different departments, and -- from the
    platform screen -- none at all. SAP (BAdI) may send none."""
    named = (department or "").strip()
    per_line = {(line.department or "").strip() for line in lines} - {""}
    if named:
        per_line.add(named)
    if len(per_line) > 1:
        raise RequestError(
            f"One request is for one department; this one names {', '.join(sorted(per_line))}. "
            "Pick one department for the whole request."
        )
    chosen = next(iter(per_line), None)
    if chosen is None and origin is Origin.PLATFORM:
        raise RequestError("Pick the department this request is for.")
    return chosen


def _answer_for(step, line: LineAnswers, line_no: int) -> dict[str, Any]:
    """The answer this line's screen gave to the step the script is asking."""
    justification = {"reason_category": line.reason_category, "free_text": line.free_text}

    if step.id in (script.I08_ASSESSMENT, script.I13_ASSESSMENT):
        allowed = {choice.value for choice in step.choices}
        if line.decision not in allowed:
            what = (
                "use the existing unit or proceed with a new one"
                if step.id == script.I08_ASSESSMENT
                else "proceed or mark it not needed"
            )
            raise RequestError(f"{line.material_id}: choose whether to {what}.", line=line_no)
        return {"choice": line.decision}
    if step.id == script.I13_CAPTURE_PLAN:
        return {
            "purpose": line.purpose,
            "planned_quantity": line.quantity,
            "window_start": line.window_start,
            "window_end": line.window_end,
        }
    if step.id == script.I13_QUANTITY:
        # Asked only when the quantity is above the suggestion -- so the
        # requester, having typed it after seeing the suggestion, kept theirs.
        return {"choice": script.KEEP_REQUESTED}
    if step.id in (script.I08_JUSTIFICATION, script.I13_QUANTITY_JUSTIFICATION):
        if not (line.reason_category and (line.free_text or "").strip()):
            first = step.prompt.split("\n")[0]
            raise RequestError(f"A reason is needed: {first}", line=line_no)
        return justification
    if step.id == script.I08_DECLARE_CHOICE:
        return {"choice": script.DECLARE if line.declaration else script.SKIP_DECLARATION}
    if step.id == script.I08_DECLARATION:
        # Replayed as the form's answer, so the same validation and recording
        # run as in the conversation.
        return dict(line.declaration or {})
    raise RequestError(f"Unexpected step {step.id} for {line.material_id}.", line=line_no)


def find_by_key(db: DbSession, idempotency_key: str) -> AssistantRequest | None:
    return db.execute(
        select(AssistantRequest).where(AssistantRequest.idempotency_key == idempotency_key)
    ).scalar_one_or_none()


def create(
    db: DbSession,
    *,
    plant: str,
    lines: list[LineAnswers],
    idempotency_key: str,
    requester: str,
    requested_for: str | None = None,
    origin: Origin = Origin.PLATFORM,
    department: str | None = None,
    department_note: str | None = None,
    today: date | None = None,
    settings: Settings | None = None,
) -> CreatedRequest:
    """Record a whole request, or nothing. See the module docstring.

    ``department`` is the request's one department. Per-line departments are
    still accepted (a screen from before 21 Oct sends them) as long as they all
    agree with it and with each other. ``department_note`` is free text beside
    it, recorded on every line as the department is."""
    settings = settings or get_settings()
    today = today or date.today()
    i13_config = get_i13_config()
    plant = (plant or "").strip()

    existing = find_by_key(db, idempotency_key)
    if existing is not None:
        return CreatedRequest(request=existing, sessions=lines_of(db, existing.id), replayed=True)

    _validate_shape(lines, plant, settings)
    department = request_department(lines, department, origin=origin)
    try:
        departments.check(
            department or "",
            from_sap=origin is Origin.BADI,
            enabled=settings.assistant_department_check_enabled,
        )
    except departments.DepartmentError as error:
        raise RequestError(str(error)) from error

    # --- 2. route and assess every line now, before anything is written ------
    prepared: list[_Prepared] = []
    skipped: list[dict[str, str]] = []
    changed: list[dict[str, Any]] = []
    for submitted, line in enumerate(lines, start=1):
        routed = route(db, line.material_id, plant)
        if not routed.in_scope:
            skipped.append({"materialId": routed.material_id, "plant": plant, "kind": "out_of_scope", "reason": routed.reason})
            continue
        try:
            assessment = _assessment_for(
                db, routed, requested_quantity=None, today=today, settings=settings, i13_config=i13_config
            )
        except SessionError as error:
            skipped.append({"materialId": routed.material_id, "plant": plant, "kind": "data_gap", "reason": str(error)})
            continue

        if routed.flow is Flow.I13 and line.check_seen and line.decision == script.PROCEED:
            fresh = uncapped_suggestion(assessment).suggested_quantity
            if not _same_quantity(line.seen_suggested_quantity, fresh):
                changed.append(
                    {
                        "materialId": routed.material_id,
                        "seenSuggestedQuantity": line.seen_suggested_quantity,
                        "suggestedQuantity": None if fresh is None else str(fresh),
                    }
                )
        prepared.append(_Prepared(len(prepared) + 1, submitted, line, routed, assessment))

    if changed:
        raise RequestConflict(changed)
    if not prepared:
        raise RequestError("None of these materials needs the assistant, so there is nothing to record.")

    # --- 3. write the request, then each line through the conversation --------
    request = AssistantRequest(
        id=ids.mint_request(settings),
        plant=plant,
        requested_for=session_module._stated(requested_for),
        requester=requester,
        origin=origin.value,
        idempotency_key=idempotency_key,
        line_count=len(prepared),
        skipped=json.dumps(skipped, sort_keys=True),
    )
    sessions: list[AssistantSession] = []
    downstream_keys: list[tuple[str, str, str]] = []
    try:
        db.add(request)
        db.flush()
        for item in prepared:
            line = item.answers
            started = session_module.start(
                db,
                material_id=item.routed.material_id,
                plant=plant,
                requester=requester,
                department=department,
                department_note=department_note,
                requested_for=requested_for,
                origin=origin,
                today=today,
                settings=settings,
                i13_config=i13_config,
                request_id=request.id,
                line_no=item.line_no,
                commit=False,
                narrate=False,
                prepared=(item.routed, item.assessment),
            )
            session = started.session
            assert session is not None and started.step is not None
            step = started.step
            for _ in range(_MAX_STEPS_PER_LINE):
                if step.is_terminal:
                    break
                payload = _answer_for(step, line, item.submitted)
                try:
                    step, _ = turn_module.answer(
                        db,
                        session.id,
                        payload,
                        actor=requester,
                        today=today,
                        settings=settings,
                        assessment=item.assessment,
                        commit=False,
                        downstream=False,
                    )
                except AnswerError as error:
                    raise RequestError(f"{item.routed.material_id}: {error}", line=item.submitted) from error
            else:  # pragma: no cover -- a script/request mismatch, not an input
                raise RequestError(f"{item.routed.material_id}: the conversation did not finish.", line=item.submitted)

            if session.flow == Flow.I13.value and script.I13_CAPTURE_PLAN in session_module.answers_of(
                session_module.turns(db, session.id)
            ):
                downstream_keys.append((session.material_id, session.plant, session.id))
            sessions.append(session)

        db.commit()
    except IntegrityError:
        # The same draft submitted twice at once: the other submit won the
        # unique idempotency key. Answer with what it recorded.
        db.rollback()
        winner = find_by_key(db, idempotency_key)
        if winner is None:
            raise
        return CreatedRequest(request=winner, sessions=lines_of(db, winner.id), replayed=True)
    except Exception:
        db.rollback()
        raise

    logger.info(
        "Assistant request %s recorded: %d lines at %s (%d skipped), by %s",
        request.id, len(sessions), plant, len(skipped), requester,
    )
    return CreatedRequest(request=request, sessions=sessions, downstream_keys=downstream_keys)


def apply_downstream(keys: list[tuple[str, str, str]]) -> None:
    """The WATCH/ACT refresh for every OAR line that captured a plan.

    The same call a completed conversation makes (``downstream.apply_capture``),
    one material at a time, on a session of its own -- run after the response
    is sent, so twenty refreshes never hold up the submit. Detection is scoped
    to one material-plant or a whole plant; there is no "these twenty", so this
    costs what twenty conversations completed one by one would have cost.
    Never raises.
    """
    if not keys:
        return
    from app.assistant.downstream import apply_capture
    from app.core.db import get_sessionmaker

    try:
        with get_sessionmaker()() as db:
            for material, plant, session_id in keys:
                apply_capture(db, material=material, plant=plant, session_id=session_id)
    except Exception:  # noqa: BLE001 -- the request is already recorded
        logger.exception("Post-request WATCH/ACT refresh failed; the next refresh repairs it")


# --- reading back ----------------------------------------------------------------


def lines_of(db: DbSession, request_id: str) -> list[AssistantSession]:
    return list(
        db.execute(
            select(AssistantSession)
            .where(AssistantSession.request_id == request_id)
            .order_by(AssistantSession.line_no)
        ).scalars()
    )


def load(db: DbSession, request_id: str, settings: Settings | None = None) -> AssistantRequest:
    """One request by reference, or :class:`RequestError` saying which rule failed."""
    try:
        parsed = ids.parse_request(request_id, settings)
    except ids.SessionIdError as error:
        raise RequestError(str(error)) from error
    request = db.get(AssistantRequest, parsed)
    if request is None:
        raise RequestError(f"No request {parsed}. The reference is well formed, so it was never issued here.")
    return request


@dataclass(frozen=True)
class LineRecord:
    """One line as recorded: what was decided, read back from its own records."""

    session: AssistantSession
    decision: str | None
    plan: ConsumptionPlanRecord | None
    suggestion: QuantitySuggestionRecord | None
    justification: Justification | None
    outcome: str


def line_records(db: DbSession, sessions: list[AssistantSession]) -> list[LineRecord]:
    ids_ = [s.id for s in sessions]
    if not ids_:
        return []

    def first_by_session(model, order_column):
        found: dict[str, Any] = {}
        for row in db.execute(select(model).where(model.session_id.in_(ids_)).order_by(order_column)).scalars():
            found.setdefault(row.session_id, row)
        return found

    plans = first_by_session(ConsumptionPlanRecord, ConsumptionPlanRecord.captured_at)
    suggestions = first_by_session(QuantitySuggestionRecord, QuantitySuggestionRecord.suggested_at)
    justifications = first_by_session(Justification, Justification.recorded_at)

    records = []
    for session in sessions:
        turns = session_module.turns(db, session.id)
        answers = session_module.answers_of(turns)
        choice = (answers.get(script.I13_ASSESSMENT) or answers.get(script.I08_ASSESSMENT) or {}).get("choice")
        if choice is None and session.flow == Flow.I08.value and turns:
            choice = script.PROCEED_NEW  # no unit existed, nothing was asked
        records.append(
            LineRecord(
                session=session,
                decision=choice,
                plan=plans.get(session.id),
                suggestion=suggestions.get(session.id),
                justification=justifications.get(session.id),
                outcome=session_module.outcome(session, turns).value,
            )
        )
    return records


def recent(db: DbSession, *, limit: int = 50) -> list[AssistantRequest]:
    return list(
        db.execute(
            select(AssistantRequest).order_by(AssistantRequest.issued_at.desc()).limit(limit)
        ).scalars()
    )


def departments_by_request(db: DbSession, request_ids: list[str]) -> dict[str, list[str]]:
    """The distinct departments each request's lines are for, in line order."""
    found: dict[str, list[str]] = {rid: [] for rid in request_ids}
    if not request_ids:
        return found
    rows = db.execute(
        select(AssistantSession.request_id, AssistantSession.department)
        .where(AssistantSession.request_id.in_(request_ids))
        .order_by(AssistantSession.request_id, AssistantSession.line_no)
    ).all()
    for request_id, department in rows:
        if department and department not in found[request_id]:
            found[request_id].append(department)
    return found


__all__ = [
    "CreatedRequest",
    "HARD_MAX_LINES",
    "LineAnswers",
    "LineRecord",
    "PreviewLine",
    "RequestConflict",
    "RequestError",
    "Suggested",
    "apply_downstream",
    "create",
    "departments_by_request",
    "find_by_key",
    "line_records",
    "lines_of",
    "load",
    "max_lines",
    "preview",
    "recent",
    "suggested_window",
    "uncapped_suggestion",
]

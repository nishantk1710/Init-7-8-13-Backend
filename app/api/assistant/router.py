"""HTTP routes for the shared reservation-time assistant, mounted at /api/assistant.

Why this is not under /api/i8 or /api/i13
------------------------------------------
The BAdI pop-up knows a material and a plant. It does **not** know whether that
material is 80-series or OAR -- that is what the router decides. So the caller
cannot choose a prefix, and a per-initiative entry point would force it to guess
before the one question it cannot answer.

One namespace for the session ID follows from the same fact. The ID comes back
off a reservation with nothing attached to say which initiative issued it.

What this module writes
------------------------
Four tables, all owned by the platform, all append-only: ``assistant_session``,
``assistant_turn``, ``consumption_plan`` and ``justification`` (plus
``quantity_suggestion``, written by the turn service). **Nothing here writes to
SAP**, and nothing in this package imports the SAP client -- the same structural
guarantee ``tests/test_i8_api.py`` asserts for I08, now widened in
``tests/test_write_paths.py`` to cover every router in the application.

Identity comes from the caller
-------------------------------
Every write takes its author from ``get_current_actor`` -- today an
``X-Actor-Id`` header, tomorrow an Entra token -- and never from the request
body. A session or a justification whose author is self-declared is not an audit
record. No request carries an author field, so there is nothing for a caller to
spoof.

``requestedFor`` on ``StartSessionRequest`` is not a hole in that. One
coordinator operates the assistant for everybody, and the name they type is who
the *part* is for -- a property of the reservation, like the material number.
It is stored in its own column, never in ``requester``, and nothing treats it as
having been authenticated.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as DbSession

from app.initiatives.i13.session_link import links_by_session, links_for_sessions
from app.models.i13_session_link import SessionReservationLink
from app.api.assistant.schemas import (
    AnswerRequest,
    AnswerResponse,
    AskRequest,
    AskResponse,
    ChoiceModel,
    DepartmentListResponse,
    DepartmentModel,
    FieldModel,
    JustificationFacets,
    JustificationListResponse,
    JustificationModel,
    JustificationPurchaseModel,
    JustificationRequest,
    LinkedReservationModel,
    MaterialMatchModel,
    MaterialSearchResponse,
    NarrativeModel,
    PlanModel,
    RoutingModel,
    SessionDeclarationModel,
    SessionListResponse,
    SessionSummary,
    SessionTraceResponse,
    StartSessionRequest,
    StartSessionResponse,
    StepModel,
    SuggestionModel,
    TurnModel,
)
from app.api.deps import Actor, get_current_actor
from app.assistant import departments
from app.assistant import intents
from app.assistant import material_index
from app.assistant import material_search
from app.assistant import session as session_service
from app.assistant import turns as turn_service
from app.assistant.models import (
    AssistantSession,
    ConsumptionPlanRecord,
    Justification,
    QuantitySuggestionRecord,
)
from app.assistant.script import reference_instruction
from app.assistant.session import Origin, SessionError
from app.assistant.steps import Step
from app.assistant.turns import AnswerError
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.initiatives.i8.config import get_i8_settings
from app.initiatives.i8.models import RepairAttestation
from app.shared.material_names import attach_material_names

#: How long the session log and trace wait for material names right after a
#: restart. Search on the session log matches names, so numbers-only rows
#: would make it silently find nothing.
NAME_WAIT_SECONDS = 10

router = APIRouter(prefix="/assistant", tags=["assistant - reservation-time"])

DbDep = Annotated[DbSession, Depends(get_db)]
ActorDep = Annotated[Actor, Depends(get_current_actor)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

#: Said on every trace rather than only when the link is missing. B2 is not a
#: bug to be discovered per-session; it is a known gap in what the platform can
#: currently prove, and a trace that stayed silent about it would read as though
#: the linkage had been checked and found absent.
LINKAGE_NOTE = (
    "No reservation is linked to this session yet. The assistant runs while the "
    "reservation is being created, so it has no document number to record. "
    "{instruction} The link is made when a SAP extract carrying it is loaded."
)
LINKED_NOTE = "Linked: the reservation's item text in SAP carries this reference."


def _linkage_note(session: AssistantSession, linked: bool) -> str:
    """The trace's link sentence, naming the same reference and the same SAP
    field as the conversation's closing message (one instruction, 28-Oct-2026)."""
    if linked:
        return LINKED_NOTE
    return LINKAGE_NOTE.format(
        instruction=reference_instruction(session.id, session.request_id)
    )


def _complete_flow() -> bool:
    """``I8_COMPLETE_FLOW_ENABLED`` (29-Oct-2026). Off, the session log, the
    trace and the justification list leave out what the I08 complete flow
    added -- the keys themselves, so a screen reads the older shape."""
    return get_i8_settings().complete_flow_enabled


def _without(
    response: BaseModel, keys: tuple[str, ...] = (), item_keys: tuple[str, ...] = ()
) -> JSONResponse:
    """``response`` as served, less ``keys`` at the top and ``item_keys`` on
    each of its ``items`` (camelCase, as on the wire)."""
    content = response.model_dump(mode="json", by_alias=True)
    for key in keys:
        content.pop(key, None)
    for item in content.get("items") or ():
        for key in item_keys:
            item.pop(key, None)
    return JSONResponse(content=content)


def _step_model(step: Step) -> StepModel:
    return StepModel(
        id=step.id,
        kind=step.kind.value,
        prompt=step.prompt,
        choices=[
            ChoiceModel(value=c.value, label=c.label, description=c.description)
            for c in step.choices
        ],
        fields=[
            FieldModel(
                name=f.name,
                label=f.label,
                type=f.type.value,
                required=f.required,
                options=[
                    ChoiceModel(value=o.value, label=o.label, description=o.description)
                    for o in f.options
                ],
                help_text=f.help_text,
                default=f.default,
            )
            for f in step.fields
        ],
        facts=step.facts,
        footnote=step.footnote,
        session_id=step.session_id,
    )


def _narrative_model(session: AssistantSession) -> NarrativeModel | None:
    """The stored narrative, where one was written.

    Read back off the session rather than taken from the writer's return value,
    so what the requester is shown is the same string the audit record holds. A
    narrative served from memory and stored separately could drift from it, and
    the stored one is the one somebody will be asked about months later.

    ``None`` whenever no narrative was written -- off, unconfigured, the stub
    provider, or the provider failed. All four are ordinary and none of them is
    an error: the deterministic advice is complete either way.
    """
    if not session.narrative:
        return None
    return NarrativeModel(
        text=session.narrative,
        prompt_id=session.narrative_prompt_id,
        prompt_version=session.narrative_prompt_version,
        model=session.narrative_model,
    )


def _routing_model(routed) -> RoutingModel:
    return RoutingModel(
        flow=routed.flow.value,
        material_id=routed.material_id,
        plant=routed.plant,
        eighty_series=routed.eighty_series,
        material_scope=routed.material_scope.value,
        mrp_type=routed.mrp_type,
        mrp_controller=routed.mrp_controller,
        also_matched=routed.also_matched.value if routed.also_matched else None,
        reason=routed.reason,
    )


MATERIAL_SEARCH_NOTE = (
    "Hint only -- the flow is decided when the session opens. Plants 1300 and "
    "1500 only."
)


@router.get(
    "/materials",
    response_model=MaterialSearchResponse,
    summary="Find a material by number or name, for the assistant's opener",
)
def search_materials(
    db: DbDep,
    q: Annotated[str, Query(max_length=80)] = "",
    limit: Annotated[
        int, Query(ge=1, le=material_search.MAX_LIMIT)
    ] = material_search.DEFAULT_LIMIT,
) -> MaterialSearchResponse:
    """Material+plant pairs the typed text could mean. Writes nothing.

    A query too short to mean anything is an empty list, not a 422: the box
    calls this as the person types, and the first keystroke is not an error.
    """
    # From memory once the copy has loaded; the database until then.
    matches = material_search.search(db, q, limit=limit, index=material_index.current())
    return MaterialSearchResponse(
        items=[
            MaterialMatchModel(
                material_id=m.material_id,
                description=m.description,
                plant=m.plant,
                plant_name=m.plant_name,
                flow_hint=m.flow_hint.value,
                mrp_controller=m.mrp_controller,
            )
            for m in matches
        ],
        note=MATERIAL_SEARCH_NOTE,
    )


@router.get(
    "/departments",
    response_model=DepartmentListResponse,
    summary="Department codes for the assistant's opener",
)
def list_departments(
    plant: Annotated[str | None, Query(max_length=8)] = None,
) -> DepartmentListResponse:
    """Every department code on VZI's department list at 1300 or 1500, named.

    The ``plant``'s own codes come first and are marked ``primary``; the rest
    are for search. Writes nothing and never touches the database on the
    request -- the list is held in memory (app/assistant/departments.py).
    """
    items = departments.for_plant(plant, wait_seconds=departments.FIRST_LOAD_WAIT_SECONDS)
    return DepartmentListResponse(
        plant=plant,
        items=[
            DepartmentModel(code=d.code, name=d.name, plants=list(d.plants), primary=d.primary)
            for d in items
        ],
        loaded=departments.is_loaded(),
    )


@router.post(
    "/sessions",
    response_model=StartSessionResponse,
    summary="Open the assistant for a material and plant",
)
def start_session(
    body: StartSessionRequest,
    db: DbDep,
    actor: ActorDep,
    settings: SettingsDep,
) -> StartSessionResponse:
    """Route the material and, if there is anything to say, mint a session.

    **A material with no flow is a 200 with a null session**, not a 404. The
    assistant genuinely has no opinion about a consumable, and the routing
    explains why -- which is what a requester who sees nothing will ask.

    A department that is not a listed code is a 422 from the platform opener
    and is stored as sent from SAP -- see app/assistant/departments.py.
    """
    try:
        departments.check(
            (body.department or "").strip(),
            from_sap=body.origin == Origin.BADI.value,
            enabled=settings.assistant_department_check_enabled,
        )
    except departments.DepartmentError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error

    try:
        started = session_service.start(
            db,
            material_id=body.material_id,
            plant=body.plant,
            # Two different people. `requester` is whoever is operating the
            # assistant, taken from the caller and never from the body;
            # `requested_for` is the name they typed for whoever wants the part.
            # They land in different columns and must not be crossed over.
            requester=actor.id,
            department=body.department,
            department_note=body.department_note,
            requested_for=body.requested_for,
            origin=Origin(body.origin),
        )
    except SessionError as error:
        # A real data gap -- an OAR part WATCH has never seen. Reported rather
        # than answered with a row of zeros, which would read as a stock
        # position rather than an absence of one.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error

    if not started.minted:
        return StartSessionResponse(routing=_routing_model(started.routed))

    assert started.session is not None and started.step is not None
    return StartSessionResponse(
        routing=_routing_model(started.routed),
        session_id=started.session.id,
        expires_at=started.session.expires_at,
        step=_step_model(started.step),
        narrative=_narrative_model(started.session),
    )


@router.post(
    "/sessions/{session_id}/turns",
    response_model=AnswerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Answer the current step and get the next one",
)
def post_turn(
    session_id: str,
    body: AnswerRequest,
    db: DbDep,
    actor: ActorDep,
) -> AnswerResponse:
    try:
        step, session = turn_service.answer(
            db, session_id, body.answer, actor=actor.id
        )
    except AnswerError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    except SessionError as error:
        # Covers both "mistyped" and "never issued", and the message says which.
        # Those are different answers -- I13's INVALID_SESSION and
        # MISSING_SESSION exist precisely to tell them apart.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error

    return AnswerResponse(session_id=session.id, step=_step_model(step))


def _linked_model(link) -> LinkedReservationModel:
    return LinkedReservationModel(
        reservation_number=link.reservation_number,
        reservation_item=link.reservation_item,
        material=link.material,
        plant=link.plant,
        source=link.source,
        sgtxt=link.sgtxt,
        first_seen_at=link.first_seen_at,
    )


def _plan_model(plan: ConsumptionPlanRecord, links: list | None = None) -> PlanModel:
    """``links``: this plan's session's reservation links, same material/plant."""
    linked = [l for l in (links or []) if (l.material, l.plant) == (plan.material, plan.plant)]
    first = linked[0] if linked else None
    return PlanModel(
        id=plan.id,
        session_id=plan.session_id,
        material=plan.material,
        plant=plan.plant,
        purpose=plan.purpose,
        planned_quantity=str(plan.planned_quantity),
        window_start=plan.window_start,
        window_end=plan.window_end,
        cost_centre=plan.cost_centre,
        order_number=plan.order_number,
        status=plan.status,
        reservation_number=plan.reservation_number or (first.reservation_number if first else None),
        reservation_item=plan.reservation_item or (first.reservation_item if first else None),
        captured_by=plan.captured_by,
        captured_at=plan.captured_at,
        linked_reservations=[_linked_model(l) for l in linked],
    )


def _suggestion_model(record: QuantitySuggestionRecord) -> SuggestionModel:
    return SuggestionModel(
        id=record.id,
        material=record.material,
        plant=record.plant,
        requested_quantity=str(record.requested_quantity),
        suggested_quantity=(
            None if record.suggested_quantity is None else str(record.suggested_quantity)
        ),
        accepted_quantity=str(record.accepted_quantity),
        suggestion_reason=record.suggestion_reason,
        months_of_cover=(
            None if record.months_of_cover is None else str(record.months_of_cover)
        ),
        cover_ceiling_months=str(record.cover_ceiling_months),
        lookback_months=record.lookback_months,
        min_history_consumptions=record.min_history_consumptions,
        consumption_count=record.consumption_count,
        is_override=record.is_override,
        suggested_at=record.suggested_at,
    )


def _justification_model(row: Justification) -> JustificationModel:
    return JustificationModel(
        id=row.id,
        session_id=row.session_id,
        exception_id=row.exception_id,
        kind=row.kind,
        reason_category=row.reason_category,
        free_text=row.free_text,
        material_id=row.material_id,
        plant=row.plant,
        author=row.author,
        recorded_at=row.recorded_at,
    )


def _declaration_model(row: RepairAttestation) -> SessionDeclarationModel:
    return SessionDeclarationModel(
        id=row.id,
        recommendation=row.recommendation,
        condition_description=row.condition_description,
        fault_category=row.fault_category,
        quantity=str(row.quantity),
        attestor_name=row.attestor_name,
        attested_at=row.attested_at,
        evidence_reference=row.evidence_reference,
        repair_document=row.repair_document,
        repair_item=row.repair_item,
        repair_document_type=row.repair_document_type,
    )


def _session_declarations(db: DbSession, session_id: str) -> list[RepairAttestation]:
    """The I08 declarations recorded in one session, newest first.

    Only the assistant writes ``session_id`` on a declaration, so these are
    exactly the ones made in the visit. The id breaks a timestamp tie so the
    order is stable.
    """
    return list(
        db.execute(
            select(RepairAttestation)
            .where(RepairAttestation.session_id == session_id)
            .order_by(RepairAttestation.attested_at.desc(), RepairAttestation.id.desc())
        ).scalars()
    )


def _declared_by_session(db: DbSession, session_ids: list[str]) -> dict[str, str]:
    """session id -> the recommendation of its newest declaration, for one page.

    One query for the page, not one per row. A session with no entry declared
    nothing.
    """
    if not session_ids:
        return {}
    rows = db.execute(
        select(
            RepairAttestation.session_id,
            RepairAttestation.recommendation,
            RepairAttestation.attested_at,
            RepairAttestation.id,
        ).where(RepairAttestation.session_id.in_(session_ids))
    ).all()
    newest: dict[str, tuple] = {}
    for session_id, recommendation, attested_at, row_id in rows:
        key = (attested_at, row_id)
        if session_id not in newest or key > newest[session_id][0]:
            newest[session_id] = (key, recommendation)
    return {session_id: value[1] for session_id, value in newest.items()}


@router.get(
    "/sessions",
    response_model=SessionListResponse,
    summary="The session log",
)
def list_sessions(
    db: DbDep,
    flow: Annotated[str | None, Query()] = None,
    material: Annotated[str | None, Query()] = None,
    plant: Annotated[str | None, Query()] = None,
    linked: Annotated[
        bool | None,
        Query(description="true: only sessions a reservation's SGTXT names; false: only those none does yet."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> SessionListResponse | JSONResponse:
    """Every session, newest first.

    The outcome on each row is derived from its turns rather than stored, so an
    abandoned session is visible here without anything having had to write
    "abandoned" to it -- which, on an append-only table, nothing could.

    Each row carries the reservations linked to it (``linked_reservations``),
    read for the page in one query. ``linked`` filters in SQL, so "not linked"
    reaches past the rows one page would load.
    """
    statement = select(AssistantSession).order_by(AssistantSession.issued_at.desc())
    if linked is not None:
        has_link = (
            select(SessionReservationLink.id)
            .where(SessionReservationLink.session_id == AssistantSession.id)
            .exists()
        )
        statement = statement.where(has_link if linked else ~has_link)
    if flow:
        statement = statement.where(AssistantSession.flow == flow.lower())
    if material:
        from app.initiatives.i8.material_number import normalise

        statement = statement.where(AssistantSession.material_id == normalise(material))
    if plant:
        statement = statement.where(AssistantSession.plant == plant.strip())

    rows = list(db.execute(statement.limit(limit)).scalars())
    links = links_for_sessions(db, (row.id for row in rows))
    complete_flow = _complete_flow()
    declared = _declared_by_session(db, [row.id for row in rows]) if complete_flow else {}
    items = []
    for row in rows:
        turn_rows = session_service.turns(db, row.id)
        items.append(
            SessionSummary(
                session_id=row.id,
                request_id=row.request_id,
                line_no=row.line_no,
                reference=row.reference,
                flow=row.flow,
                outcome=session_service.outcome(row, turn_rows).value,
                material_id=row.material_id,
                plant=row.plant,
                department=row.department,
                department_name=departments.name_for(row.department),
                department_note=row.department_note,
                requested_for=row.requested_for,
                requester=row.requester,
                origin=row.origin,
                issued_at=row.issued_at,
                expires_at=row.expires_at,
                turns=len(turn_rows),
                linked_reservations=[_linked_model(link) for link in links.get(row.id, [])],
                declared=row.id in declared,
                declaration_recommendation=declared.get(row.id),
            )
        )

    attach_material_names(items, field="material_id", wait_seconds=NAME_WAIT_SECONDS)
    response = SessionListResponse(
        items=items,
        total=len(items),
        note=(
            "Outcome is derived from each session's turns, never stored -- the "
            "table is append-only, so there is nowhere to write a status after "
            "the fact. ABANDONED means advice was served and the conversation "
            "was not finished, which is a number both FRSs want rather than an "
            "error."
        ),
    )
    if complete_flow:
        return response
    return _without(response, item_keys=("declared", "declarationRecommendation"))


@router.get(
    "/sessions/{session_id}",
    response_model=SessionTraceResponse,
    summary="One session, and everything it produced",
)
def get_session(session_id: str, db: DbDep) -> SessionTraceResponse | JSONResponse:
    """The FR-8 trace.

    ``assessment`` is replayed from the stored JSON, not recomputed. That is the
    whole point of storing it: the register moves and stock moves, so an answer
    regenerated now is not the answer somebody acted on then.
    """
    try:
        session = session_service.load(db, session_id)
    except SessionError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error

    turn_rows = session_service.turns(db, session.id)

    plans = list(
        db.execute(
            select(ConsumptionPlanRecord)
            .where(ConsumptionPlanRecord.session_id == session.id)
            .order_by(ConsumptionPlanRecord.captured_at)
        ).scalars()
    )
    suggestions = list(
        db.execute(
            select(QuantitySuggestionRecord)
            .where(QuantitySuggestionRecord.session_id == session.id)
            .order_by(QuantitySuggestionRecord.suggested_at)
        ).scalars()
    )
    justifications = list(
        db.execute(
            select(Justification)
            .where(Justification.session_id == session.id)
            .order_by(Justification.recorded_at)
        ).scalars()
    )

    links = links_by_session(db).get(session.id, [])

    trace = SessionTraceResponse(
        session_id=session.id,
        request_id=session.request_id,
        line_no=session.line_no,
        reference=session.reference,
        flow=session.flow,
        outcome=session_service.outcome(session, turn_rows).value,
        material_id=session.material_id,
        plant=session.plant,
        department=session.department,
        department_name=departments.name_for(session.department),
        department_note=session.department_note,
        requested_for=session.requested_for,
        requested_quantity=(
            None if session.requested_quantity is None else str(session.requested_quantity)
        ),
        requester=session.requester,
        origin=session.origin,
        issued_at=session.issued_at,
        expires_at=session.expires_at,
        expired=session_service.is_expired(session),
        routing_reason=session.routing_reason,
        assessment=json.loads(session.assessment),
        narrative=session.narrative,
        turns=[
            TurnModel(
                sequence=t.sequence,
                step_id=t.step_id,
                step_kind=t.step_kind,
                question=t.question,
                answer=json.loads(t.answer) if t.answer else None,
                actor=t.actor,
                answered_at=t.answered_at,
            )
            for t in turn_rows
        ],
        plans=[_plan_model(p, links) for p in plans],
        quantity_suggestions=[_suggestion_model(s) for s in suggestions],
        justifications=[_justification_model(j) for j in justifications],
        declarations=(
            [_declaration_model(d) for d in _session_declarations(db, session.id)]
            if _complete_flow()
            else []
        ),
        linked_reservations=[_linked_model(l) for l in links],
        linkage_note=_linkage_note(session, bool(links)),
    )
    attach_material_names([trace], field="material_id", wait_seconds=NAME_WAIT_SECONDS)
    if _complete_flow():
        return trace
    return _without(trace, keys=("declarations",))


# --- Justifications, shared by both initiatives ---------------------------
#
# Mounted at /api/justifications rather than under either initiative, for the
# same reason the session table is shared: both FRSs describe the same record,
# and two tables with two endpoints would drift.

justifications_router = APIRouter(prefix="/justifications", tags=["justifications"])


@justifications_router.post(
    "",
    response_model=JustificationModel,
    status_code=status.HTTP_201_CREATED,
    summary="Record a justification (I08 FR-7 / I13 FR-7)",
)
def post_justification(
    body: JustificationRequest,
    db: DbDep,
    actor: ActorDep,
    settings: SettingsDep,
) -> JustificationModel:
    """Record why somebody went ahead anyway.

    The reason category is validated against configuration rather than an enum,
    because VZI has not supplied the vocabulary yet (open question 9) -- an enum
    would need a migration on the day they do.
    """
    category = body.reason_category.strip().upper()
    allowed = settings.assistant_justification_reason_category_list
    if category not in allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"reasonCategory {body.reason_category!r} is not in the configured "
                f"list: {', '.join(allowed)}. These are placeholders until VZI's "
                "own vocabulary is confirmed."
            ),
        )

    free_text = body.free_text.strip()
    if not free_text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "freeText is required -- a category on its own records that a box "
                "was ticked, not that anybody thought about it."
            ),
        )

    from app.initiatives.i8.material_number import is_eighty_series, normalise
    from app.shared.plant_scope import IN_SCOPE_PLANTS, is_in_scope

    # The table is append-only, so a justification for a plant the platform
    # does not serve, or a new purchase of a part that is not repairable, would
    # be a permanent row nothing could ever read correctly.
    if not is_in_scope(body.plant):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"plant {body.plant!r} is outside the platform's scope: "
                f"{', '.join(IN_SCOPE_PLANTS)}"
            ),
        )
    if body.kind == "NEW_ACQUISITION" and not is_eighty_series(
        normalise(body.material_id)
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"materialId {body.material_id!r} is not an 80-series repairable "
                "part. A NEW_ACQUISITION justification answers the I08 challenge, "
                "which only 80-series parts receive."
            ),
        )

    if body.session_id is not None:
        # Validated so a justification cannot be hung off an ID that was never
        # issued. The table is append-only, so an orphan is permanent.
        try:
            session_service.load(db, body.session_id)
        except SessionError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
            ) from error

    justification = Justification(
        session_id=body.session_id,
        exception_id=body.exception_id,
        kind=body.kind,
        reason_category=category,
        free_text=free_text,
        # Normalised -- ruling 5.1, the same rule the attestation follows.
        material_id=normalise(body.material_id) or body.material_id,
        plant=body.plant.strip(),
        author=actor.id,
    )
    db.add(justification)
    db.commit()
    db.refresh(justification)
    return _justification_model(justification)


def _requester_names(db: DbSession, session_ids: list[str | None]) -> dict[str, str]:
    """session id -> the requester name typed on it, for one page in one query.

    ``requested_for`` is the name the operator typed for whoever wants the
    part. It is not identity, but until sign-in is wired it is the only name a
    chat or request reason has: its author is the anonymous placeholder.
    """
    wanted = sorted({sid for sid in session_ids if sid})
    if not wanted:
        return {}
    rows = db.execute(
        select(AssistantSession.id, AssistantSession.requested_for).where(
            AssistantSession.id.in_(wanted)
        )
    ).all()
    return {sid: name.strip() for sid, name in rows if name and name.strip()}


def _purchases_answered(
    rows: list[Justification],
) -> dict[str, JustificationPurchaseModel] | None:
    """justification id -> the new purchase it answers, for NEW_ACQUISITION rows.

    Read from the I08 snapshot already in memory, the same new purchases the
    bought-new check reads; never builds or waits for one. None while no
    snapshot has been built (just after a restart), so the caller can say
    nothing was looked up rather than that nothing was bought.
    """
    from app.initiatives.i8.acquisitions import NEW_ACQUISITION_KIND, JustificationRecord
    from app.initiatives.i8.config import get_i8_settings
    from app.initiatives.i8.exceptions import purchases_answered
    from app.initiatives.i8.material_number import normalise
    from app.initiatives.i8.service import cached_snapshot

    snapshot = cached_snapshot()
    if snapshot is None:
        return None
    records = [
        JustificationRecord(
            material_id=normalise(row.material_id) or row.material_id,
            plant=(row.plant or "").strip(),
            recorded_on=row.recorded_at.date(),
            exception_id=row.exception_id,
            id=row.id,
        )
        for row in rows
        if row.kind == NEW_ACQUISITION_KIND
    ]
    if not records:
        return {}
    answered = purchases_answered(
        records,
        getattr(snapshot, "acquisitions", ()),
        window_days=get_i8_settings().justification_window_days,
    )
    return {
        justification_id: JustificationPurchaseModel(
            type=acquisition.document_type,
            document_number=acquisition.purchasing_document,
            line=acquisition.item,
            raised_at=acquisition.raised_at,
        )
        for justification_id, acquisition in answered.items()
    }


@justifications_router.get(
    "",
    response_model=JustificationListResponse,
    summary="Justifications, newest first",
)
def list_justifications(
    db: DbDep,
    settings: SettingsDep,
    session_id: Annotated[str | None, Query()] = None,
    exception_id: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
    material: Annotated[str | None, Query()] = None,
    plant: Annotated[str | None, Query()] = None,
    reason_category: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JustificationListResponse | JSONResponse:
    """One page of justifications, newest first, with the count and the filter
    values over every match.

    ``total`` used to be the length of the page, so a screen showing it stopped
    at whatever ``limit`` it asked for. It is now a count over the filters, so a
    paged screen knows how many pages there are. ``offset`` and ``limit`` page
    through the rest.

    ``q`` is a case-insensitive substring search over the fields a reader sees
    on a card: material, free text, author, session and reason. A space in it
    also matches the underscore in a reason, so "urgent breakdown" finds
    ``URGENT_BREAKDOWN`` the way the card prints it.
    """
    conditions = []
    if session_id:
        from app.assistant import ids

        conditions.append(Justification.session_id == ids.normalise(session_id))
    if exception_id:
        conditions.append(Justification.exception_id == exception_id)
    if kind:
        conditions.append(Justification.kind == kind.strip().upper())
    if material:
        from app.initiatives.i8.material_number import normalise

        conditions.append(Justification.material_id == normalise(material))
    if plant:
        conditions.append(Justification.plant == plant.strip())
    if reason_category:
        conditions.append(Justification.reason_category == reason_category.strip())
    needle = (q or "").strip()
    if needle:
        conditions.append(
            or_(
                Justification.material_id.icontains(needle, autoescape=True),
                Justification.free_text.icontains(needle, autoescape=True),
                Justification.author.icontains(needle, autoescape=True),
                Justification.session_id.icontains(needle, autoescape=True),
                Justification.reason_category.icontains(
                    needle.replace(" ", "_"), autoescape=True
                ),
            )
        )

    # The id breaks ties. Two records with the same timestamp could otherwise
    # come back in either order, and one would show on two pages while the
    # other showed on none.
    statement = (
        select(Justification)
        .where(*conditions)
        .order_by(Justification.recorded_at.desc(), Justification.id.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = list(db.execute(statement).scalars())
    total = db.execute(
        select(func.count()).select_from(Justification).where(*conditions)
    ).scalar_one()
    items = [_justification_model(row) for row in rows]
    names = _requester_names(db, [row.session_id for row in rows])
    complete_flow = _complete_flow()
    purchases = _purchases_answered(rows) if complete_flow else None
    for item in items:
        item.requester_name = names.get(item.session_id or "")
        if purchases is not None and item.id in purchases:
            item.purchase = purchases[item.id]
    facets = JustificationFacets(
        plants=sorted(
            db.execute(select(Justification.plant).where(*conditions).distinct()).scalars()
        ),
        reason_categories=sorted(
            db.execute(
                select(Justification.reason_category).where(*conditions).distinct()
            ).scalars()
        ),
    )
    response = JustificationListResponse(
        items=items,
        total=total,
        purchases_checked=purchases is not None,
        facets=facets,
        reason_categories=settings.assistant_justification_reason_category_list,
        note=(
            "Reason categories are configuration, not an enum -- both FRSs ask "
            "for 'a reason category plus free text' and neither lists the "
            "categories. The list served here is a placeholder until VZI "
            "supplies its own."
        ),
    )
    if complete_flow:
        return response
    # The purchase each reason answers came with the complete flow; the typed
    # requester name stays.
    return _without(response, keys=("purchasesChecked",), item_keys=("purchase",))


# --- The free-text box (section 4.6) --------------------------------------
#
# A small, explicit set of intents over deterministic read models. No model is
# involved in this path. General question-answering over the whole dataset is a
# SEPARATE scope item that has not been agreed, and every refusal here says so
# rather than letting the boundary blur.


@router.post(
    "/ask",
    response_model=AskResponse,
    summary="Ask one of a fixed set of business questions",
)
def ask(body: AskRequest, db: DbDep) -> AskResponse:
    """Answer from the platform's own read models, or say plainly that it cannot.

    Never a 4xx for an unrecognised question: "I do not know" is a valid answer
    and the commonest one, and a 422 would make the frontend render a failure
    where the honest response is a sentence.
    """
    result = intents.answer(db, body.question)
    return AskResponse(
        intent=result.intent.value,
        answered=result.answered,
        text=result.text,
        sources=list(result.sources),
        data=result.data,
        suggestions=list(result.suggestions),
        note=result.note,
    )


@router.get(
    "/ask/suggestions",
    response_model=list[str],
    summary="The questions the free-text box can actually answer",
)
def ask_suggestions() -> list[str]:
    """What to put in the suggestion chips above the input.

    Served rather than hard-coded in the frontend so the chips cannot offer a
    question the backend has stopped answering.
    """
    return list(intents.ANSWERABLE)

"""/api/assistant/requests -- several materials checked in one go.

Off unless ``ASSISTANT_REQUESTS_ENABLED`` is set: every route answers 404 until
then. The rules live in :mod:`app.assistant.requests`; this module only turns
them into HTTP.

* ``POST /assistant/requests/preview`` -- what each material would ask. Writes
  nothing.
* ``POST /assistant/requests`` -- record the whole request, or nothing.
  422 names the line at fault; 409 lists the suggestions that moved since the
  preview, with their fresh values.
* ``GET /assistant/requests`` / ``GET /assistant/requests/{id}`` -- read back.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.api.assistant.request_schemas import (
    CreateRequestBody,
    JustificationBriefModel,
    PreviewLineModel,
    PreviewRequest,
    PreviewResponse,
    RequestLineModel,
    RequestListResponse,
    RequestResponse,
    RequestSummaryModel,
    SkippedModel,
    SuggestedModel,
)
from app.api.assistant.schemas import LinkedReservationModel
from app.api.deps import Actor, get_current_actor
from app.assistant import requests as request_service
from app.assistant.models import AssistantRequest, AssistantSession
from app.assistant.session import Origin
from app.core.config import Settings, get_settings
from app.core.db import get_db
from app.shared.material_names import material_names

#: How long a read waits for material names right after a restart.
NAME_WAIT_SECONDS = 10


def _enabled(settings: Annotated[Settings, Depends(get_settings)]) -> None:
    if not settings.assistant_requests_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


router = APIRouter(
    prefix="/assistant/requests",
    tags=["assistant - multi-material requests"],
    dependencies=[Depends(_enabled)],
)

DbDep = Annotated[DbSession, Depends(get_db)]
ActorDep = Annotated[Actor, Depends(get_current_actor)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _names(materials: list[str]) -> dict[str, str]:
    from app.shared import material_names as names_module

    names_module.wait_for_first_load(NAME_WAIT_SECONDS)
    return material_names(materials)


@router.post("/preview", response_model=PreviewResponse, summary="What each material would ask")
def preview(body: PreviewRequest, db: DbDep, settings: SettingsDep) -> PreviewResponse:
    lines = request_service.preview(
        db, [(m.material_id, m.plant) for m in body.materials], settings=settings
    )
    names = _names([line.material_id for line in lines])
    return PreviewResponse(
        max_lines=request_service.max_lines(settings),
        lines=[
            PreviewLineModel(
                material_id=line.material_id,
                material_description=names.get(line.material_id),
                plant=line.plant,
                kind=line.kind,
                reason=line.reason,
                facts=line.facts,
                unit_exists=line.unit_exists,
                also_matched=line.also_matched,
                suggestion=(
                    SuggestedModel(
                        quantity=None if line.suggestion.quantity is None else str(line.suggestion.quantity),
                        reason=line.suggestion.reason,
                        window_start=line.suggestion.window_start,
                        window_end=line.suggestion.window_end,
                        window_months=line.suggestion.window_months,
                    )
                    if line.suggestion is not None
                    else None
                ),
            )
            for line in lines
        ],
    )


@router.post(
    "",
    response_model=RequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a multi-material request -- every line, or nothing",
)
def create_request(
    body: CreateRequestBody,
    background: BackgroundTasks,
    db: DbDep,
    actor: ActorDep,
    settings: SettingsDep,
) -> RequestResponse:
    lines = [
        request_service.LineAnswers(
            material_id=line.material_id,
            plant=line.plant,
            department=line.department,
            decision=line.decision,
            quantity=line.quantity,
            window_start=line.window_start,
            window_end=line.window_end,
            purpose=line.purpose,
            reason_category=line.reason_category,
            free_text=line.free_text,
            seen_suggested_quantity=line.seen_suggested_quantity,
            # Sent at all, as opposed to sent as null ("no suggestion was shown").
            check_seen="seen_suggested_quantity" in line.model_fields_set,
        )
        for line in body.lines
    ]
    try:
        created = request_service.create(
            db,
            plant=body.plant,
            lines=lines,
            idempotency_key=body.idempotency_key,
            requester=actor.id,
            requested_for=body.requested_for,
            origin=Origin(body.origin),
            settings=settings,
        )
    except request_service.RequestConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": str(error), "changed": error.changed},
        ) from error
    except request_service.RequestError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": str(error), "line": error.line},
        ) from error

    # After the response: the per-material WATCH/ACT refresh the conversation
    # does inline. Twenty of them must not hold up the submit.
    background.add_task(request_service.apply_downstream, created.downstream_keys)
    return _request_response(db, created.request, replayed=created.replayed)


@router.get("", response_model=RequestListResponse, summary="Recent requests, newest first")
def list_requests(
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> RequestListResponse:
    rows = request_service.recent(db, limit=limit)
    departments = request_service.departments_by_request(db, [r.id for r in rows])
    return RequestListResponse(
        items=[
            RequestSummaryModel(
                request_id=r.id,
                plant=r.plant,
                requested_for=r.requested_for,
                issued_at=r.issued_at,
                line_count=r.line_count,
                skipped_count=len(json.loads(r.skipped or "[]")),
                departments=departments.get(r.id, []),
            )
            for r in rows
        ]
    )


@router.get("/{request_id}", response_model=RequestResponse, summary="One request, every line")
def get_request(request_id: str, db: DbDep, settings: SettingsDep) -> RequestResponse:
    try:
        request = request_service.load(db, request_id, settings)
    except request_service.RequestError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return _request_response(db, request)


def _request_response(db: DbSession, request: AssistantRequest, *, replayed: bool = False) -> RequestResponse:
    from app.models.i13_session_link import SessionReservationLink

    sessions = request_service.lines_of(db, request.id)
    records = request_service.line_records(db, sessions)
    names = _names([s.material_id for s in sessions])

    links: dict[str, list[SessionReservationLink]] = {}
    if sessions:
        for link in db.execute(
            select(SessionReservationLink).where(SessionReservationLink.session_id.in_([s.id for s in sessions]))
        ).scalars():
            links.setdefault(link.session_id, []).append(link)

    def quantity(value) -> str | None:  # noqa: ANN001
        # The columns are Numeric(18, 3): "8.000" reads as a precision nobody
        # typed. Shown as the number it is.
        return None if value is None else format(value.normalize(), "f")

    lines = []
    for record in records:
        session: AssistantSession = record.session
        plan, suggestion, justification = record.plan, record.suggestion, record.justification
        lines.append(
            RequestLineModel(
                line_no=session.line_no or 0,
                reference=session.reference,
                session_id=session.id,
                flow=session.flow,
                outcome=record.outcome,
                material_id=session.material_id,
                material_description=names.get(session.material_id),
                plant=session.plant,
                department=session.department,
                decision=record.decision,
                planned_quantity=quantity(plan.planned_quantity) if plan else None,
                suggested_quantity=quantity(suggestion.suggested_quantity) if suggestion else None,
                accepted_quantity=quantity(suggestion.accepted_quantity) if suggestion else None,
                window_start=plan.window_start if plan else None,
                window_end=plan.window_end if plan else None,
                purpose=plan.purpose if plan else None,
                justification=(
                    JustificationBriefModel(
                        kind=justification.kind,
                        reason_category=justification.reason_category,
                        free_text=justification.free_text,
                    )
                    if justification
                    else None
                ),
                linked_reservations=[
                    LinkedReservationModel(
                        reservation_number=l.reservation_number,
                        reservation_item=l.reservation_item,
                        material=l.material,
                        plant=l.plant,
                        source=l.source,
                        sgtxt=l.sgtxt,
                        first_seen_at=l.first_seen_at,
                    )
                    for l in links.get(session.id, [])
                ],
            )
        )

    departments = request_service.departments_by_request(db, [request.id]).get(request.id, [])
    return RequestResponse(
        request_id=request.id,
        plant=request.plant,
        requested_for=request.requested_for,
        requester=request.requester,
        origin=request.origin,
        issued_at=request.issued_at,
        departments=departments,
        lines=lines,
        skipped=[SkippedModel(**{
            "material_id": s["materialId"], "plant": s["plant"], "kind": s["kind"], "reason": s["reason"],
        }) for s in json.loads(request.skipped or "[]")],
        replayed=replayed,
    )

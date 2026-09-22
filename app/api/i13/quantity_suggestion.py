"""W7.4 quantity-suggestion API (FR-3).

Five routes: issue a suggestion, read one, list them, capture an override
justification, and record acceptance. Thin, like every other I13 route --
the arithmetic is in ``app.initiatives.i13.quantity_suggestion`` and the
persistence in ``app.initiatives.i13.quantity_suggestion_store``.

Unlike ``/watch`` and ``/consumption-attribution``, POSTing here is *not*
side-effect-free: issuing a suggestion persists it, because FRS §8 needs the
record of what was put in front of the requester and whether they took it.
Reads stay read-only.

The list route is paginated in SQL. Several older I13 list endpoints return
unbounded payloads; this one does not repeat that.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.i13.deps import Actor, get_current_actor
from app.core.db import get_db
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.quantity_suggestion_store import (
    WatchMetricNotFoundError,
    add_justification,
    get_quantity_suggestion,
    issue_quantity_suggestion,
    list_justifications,
    list_quantity_suggestions,
    record_acceptance,
)
from app.schemas.i13_quantity import (
    QuantityAcceptanceRequest,
    QuantityJustificationRequest,
    QuantityJustificationResponse,
    QuantitySuggestionDetailResponse,
    QuantitySuggestionRequest,
    QuantitySuggestionResponse,
)

router = APIRouter(prefix="/quantity-suggestion", tags=["i13-quantity"])


def _detail(db: Session, record) -> QuantitySuggestionDetailResponse:
    return QuantitySuggestionDetailResponse(
        **QuantitySuggestionResponse.model_validate(record).model_dump(),
        justifications=[
            QuantityJustificationResponse.model_validate(j) for j in list_justifications(db, record.suggestion_id)
        ],
    )


@router.post("", response_model=QuantitySuggestionResponse, status_code=201)
def create_quantity_suggestion(
    payload: QuantitySuggestionRequest,
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
) -> QuantitySuggestionResponse:
    """Compute and persist one suggestion.

    Always 201, even where the engine declined: NO_SUGGESTION with an
    explicit reason code is a real, recorded answer (and the one FRS §3.1
    demands where history is thin), not an error. The client reads
    ``direction``/``reason_code`` to know which it got.

    This request boundary is where the wall clock is read; everything past
    it takes ``as_of`` as a plain argument, the same as W6.6's detection run.
    """
    try:
        record = issue_quantity_suggestion(
            db,
            config.quantity_suggestion,
            material=payload.material,
            plant=payload.plant,
            requested_quantity=payload.requested_quantity,
            plan_window_months=payload.plan_window_months,
            as_of=datetime.now(timezone.utc),
            session_id=payload.session_id,
            reservation_number=payload.reservation_number,
            reservation_item=payload.reservation_item,
            requester_id=payload.requester_id,
        )
    except WatchMetricNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"{exc} -- run the W6.3 WATCH mart refresh for this material/plant first",
        ) from exc
    db.commit()
    return QuantitySuggestionResponse.model_validate(record)


@router.get("", response_model=list[QuantitySuggestionResponse])
def list_suggestions(
    material: str | None = Query(None),
    plant: str | None = Query(None),
    session_id: str | None = Query(None),
    reservation_number: str | None = Query(None),
    direction: str | None = Query(None, description="UP / DOWN / NONE / NO_SUGGESTION"),
    reason_code: str | None = Query(None),
    accepted: bool | None = Query(None, description="Omit for all; note NULL means undecided, not rejected."),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[QuantitySuggestionResponse]:
    rows = list_quantity_suggestions(
        db,
        material=material,
        plant=plant,
        session_id=session_id,
        reservation_number=reservation_number,
        direction=direction,
        reason_code=reason_code,
        accepted=accepted,
        limit=limit,
        offset=offset,
    )
    return [QuantitySuggestionResponse.model_validate(row) for row in rows]


@router.get("/{suggestion_id}", response_model=QuantitySuggestionDetailResponse)
def get_suggestion(suggestion_id: str, db: Session = Depends(get_db)) -> QuantitySuggestionDetailResponse:
    record = get_quantity_suggestion(db, suggestion_id)
    if record is None:
        raise HTTPException(status_code=404, detail="quantity suggestion not found")
    return _detail(db, record)


@router.post("/{suggestion_id}/justification", response_model=QuantitySuggestionDetailResponse)
def create_justification(
    suggestion_id: str,
    payload: QuantityJustificationRequest,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
) -> QuantitySuggestionDetailResponse:
    """Capture why the requester is keeping a different quantity.

    Structured category plus free text -- the same shape W6.6's requester
    confirmation uses, so the initiative has one justification vocabulary.
    Append-only: a revised reason adds a row and the earlier one stays
    readable.
    """
    try:
        add_justification(
            db,
            suggestion_id,
            reason_category=payload.reason_category,
            free_text=payload.free_text,
            actor_id=actor.id,
            as_of=datetime.now(timezone.utc),
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="quantity suggestion not found") from None
    db.commit()
    record = get_quantity_suggestion(db, suggestion_id)
    return _detail(db, record)


@router.post("/{suggestion_id}/acceptance", response_model=QuantitySuggestionResponse)
def create_acceptance(
    suggestion_id: str,
    payload: QuantityAcceptanceRequest,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
) -> QuantitySuggestionResponse:
    """Record whether the requester took the suggestion.

    This is what FRS §8 counts a saving against, and it is also what makes
    the decision visible to W6.6: an undecided suggestion raises no
    quantity-override exception, and a rejected one does (see
    ``quantity_suggestion_store.build_quantity_decision_records``).
    """
    try:
        record = record_acceptance(
            db,
            suggestion_id,
            accepted=payload.accepted,
            actor_id=actor.id,
            as_of=datetime.now(timezone.utc),
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="quantity suggestion not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return QuantitySuggestionResponse.model_validate(record)

"""I13's two WS7 endpoints: the consumption-plan write path, and FR-3 on its own.

These live under ``/api/i13`` rather than ``/api/assistant`` because, unlike the
session entry point, the caller already knows which initiative it wants. A
consumption plan is an Initiative 13 record and the quantity suggestion is an
Initiative 13 rule; only the *routing* question needed a shared prefix.

The write path ``plans.py`` never had
--------------------------------------
``app/initiatives/i13/plans.py`` reads ``consumption_plans.csv`` -- 742
fabricated rows with invented ``SESS-000001`` identifiers -- and its own
docstring is honest that I13 "does not generate or fabricate a plan when none
exists". This is the endpoint that produces real ones.

A plan captured here requires a session. That is not bureaucracy: FR-4 asks for
traceability from a plan back to the advice that shaped it, and a plan with no
session has no origin to trace. The assistant's own flow captures plans through
the conversation; this endpoint exists for a caller that already holds a session
and wants to record a plan directly.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.api.assistant.schemas import PlanModel
from app.api.i13.deps import Actor, get_current_actor, get_data_dir
from app.assistant import session as session_service
from app.assistant.models import ConsumptionPlanRecord
from app.assistant.session import SessionError
from app.core.db import get_db
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.quantity import build_quantity_config, suggest
from app.initiatives.i13.watch import compute_watch_metrics
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository

router = APIRouter()

DbDep = Annotated[DbSession, Depends(get_db)]
ActorDep = Annotated[Actor, Depends(get_current_actor)]


class I13AssistantModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ConsumptionPlanRequest(I13AssistantModel):
    """A plan to capture. ``capturedBy`` is absent -- it comes from the caller.

    Quantities are strings for the same reason they are everywhere else in this
    API: a JSON number round-trips through a float, and this value reaches an
    append-only record that a compliance engine reads.
    """

    session_id: str
    purpose: str
    planned_quantity: str
    window_start: date | None = None
    window_end: date | None = None
    cost_centre: str | None = None
    order_number: str | None = None


class QuantitySuggestionResponse(I13AssistantModel):
    """FR-3's answer.

    ``suggestedQuantity`` is null when no suggestion could be made -- **not
    zero**. ``available`` says which, so a caller never has to infer it from a
    missing number, and ``reason`` explains it in a sentence.
    """

    material: str
    plant: str
    requested_quantity: str
    suggested_quantity: str | None = None
    available: bool
    is_override: bool
    reason: str
    no_suggestion_reason: str | None = None
    stock_on_hand: str | None = None
    open_po_quantity: str
    average_monthly_consumption: str
    months_of_cover: str | None = None
    projected_cover_if_suggested: str | None = None
    projected_cover_if_requested: str | None = None
    consumption_count: int
    cover_ceiling_months: str
    lookback_months: int
    min_history_consumptions: int
    basis_note: str


def _decimal(raw: str, label: str) -> Decimal:
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{label} must be a number, got {raw!r}",
        ) from error
    if value <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{label} must be greater than zero, got {value}",
        )
    return value


@router.post(
    "/consumption-plans",
    response_model=PlanModel,
    status_code=status.HTTP_201_CREATED,
    summary="FR-4: capture a consumption plan against a session",
)
def post_consumption_plan(
    body: ConsumptionPlanRequest,
    db: DbDep,
    actor: ActorDep,
) -> PlanModel:
    """Record what the requester says the material is for.

    The FRS-complete plan is captured -- a planned **window**, a cost centre and
    an order where known -- while today's ACT detection still reads it narrowly.
    That was a deliberate choice between three options: capturing only what ACT
    reads would have been faster and would have under-captured against the FRS
    permanently, and this table is append-only so it could never be backfilled.
    """
    try:
        session = session_service.load(db, body.session_id)
    except SessionError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(error)
        ) from error

    purpose = body.purpose.strip()
    if not purpose:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="purpose is required -- it is the part a human reads.",
        )

    if body.window_start and body.window_end and body.window_end < body.window_start:
        # Would make FR-7's "window end plus grace" breach fire immediately,
        # against somebody who meant the opposite.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"the planned window ends ({body.window_end}) before it starts "
                f"({body.window_start})"
            ),
        )

    plan = ConsumptionPlanRecord(
        session_id=session.id,
        # Null, and normally so: the reservation does not exist in SAP yet. See
        # FR-8 and blocker B2.
        reservation_number=None,
        reservation_item=None,
        material=session.material_id,
        plant=session.plant,
        purpose=purpose,
        planned_quantity=_decimal(body.planned_quantity, "plannedQuantity"),
        window_start=body.window_start,
        window_end=body.window_end,
        cost_centre=(body.cost_centre or "").strip() or None,
        order_number=(body.order_number or "").strip() or None,
        # "OPEN", not a vocabulary of our own. Detection tests
        # `plan.status != "OPEN"` before it will treat a plan as a live
        # commitment, and the reference CSV uses OPEN/CLOSED. A captured
        # plan written as "ACTIVE" parsed as a plan that had been
        # withdrawn -- found by the step-8 end-to-end test, which is
        # exactly the class of mistake it exists to catch.
        status="OPEN",
        captured_by=actor.id,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)

    return PlanModel(
        id=plan.id,
        material=plan.material,
        plant=plan.plant,
        purpose=plan.purpose,
        planned_quantity=str(plan.planned_quantity),
        window_start=plan.window_start,
        window_end=plan.window_end,
        cost_centre=plan.cost_centre,
        order_number=plan.order_number,
        status=plan.status,
        reservation_number=plan.reservation_number,
        reservation_item=plan.reservation_item,
        captured_by=plan.captured_by,
        captured_at=plan.captured_at,
    )


@router.get(
    "/consumption-plans",
    response_model=list[PlanModel],
    summary="Captured consumption plans (the real ones, not the CSV)",
)
def list_consumption_plans(
    db: DbDep,
    session_id: Annotated[str | None, Query()] = None,
    material: Annotated[str | None, Query()] = None,
    plant: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[PlanModel]:
    """Plans this platform captured.

    Deliberately **not** merged with the 742 rows in
    ``consumption_plans.csv``. Those are fabricated, and a list that blended
    them with real captures would make it impossible to tell at a glance which
    is which -- which is the single most important thing to be able to say
    before anybody demos the exception queue.
    """
    statement = select(ConsumptionPlanRecord).order_by(
        ConsumptionPlanRecord.captured_at.desc()
    )
    if session_id:
        from app.assistant import ids

        statement = statement.where(
            ConsumptionPlanRecord.session_id == ids.normalise(session_id)
        )
    if material:
        from app.initiatives.i8.material_number import normalise

        statement = statement.where(ConsumptionPlanRecord.material == normalise(material))
    if plant:
        statement = statement.where(ConsumptionPlanRecord.plant == plant.strip())

    return [
        PlanModel(
            id=p.id,
            material=p.material,
            plant=p.plant,
            purpose=p.purpose,
            planned_quantity=str(p.planned_quantity),
            window_start=p.window_start,
            window_end=p.window_end,
            cost_centre=p.cost_centre,
            order_number=p.order_number,
            status=p.status,
            reservation_number=p.reservation_number,
            reservation_item=p.reservation_item,
            captured_by=p.captured_by,
            captured_at=p.captured_at,
        )
        for p in db.execute(statement.limit(limit)).scalars()
    ]


@router.get(
    "/quantity-suggestion",
    response_model=QuantitySuggestionResponse,
    summary="FR-3: how many should they reserve?",
)
def get_quantity_suggestion(
    db: DbDep,
    material: Annotated[str, Query()],
    plant: Annotated[str, Query()],
    quantity: Annotated[str, Query(description="What the requester wants to reserve")],
    config: Annotated[I13Config, Depends(get_i13_config)],
    data_dir: Annotated[Path, Depends(get_data_dir)],
) -> QuantitySuggestionResponse:
    """The suggestion on its own, without a conversation.

    Reads WATCH the same way ``/api/i13/watch`` does, so the numbers behind a
    suggestion and the numbers on the WATCH screen are the same numbers -- there
    is no second computation here to disagree with the first.
    """
    from app.initiatives.i8.material_number import normalise

    material_key = normalise(material) or material
    plant_key = plant.strip()

    metrics = compute_watch_metrics(
        PostgresMovementRepository(db),
        PostgresProcurementRepository(db),
        PostgresReservationRepository(db),
        fetch_material_scope_index(db, material=material_key, plant=plant_key),
        config,
        data_dir,
        material=material_key,
        plant=plant_key,
        db=db,
    )
    metric = next(
        (m for m in metrics if m.material == material_key and m.plant == plant_key),
        None,
    )
    if metric is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"No WATCH row for {material_key} at plant {plant_key} -- no "
                "movement or ledger activity has ever been recorded for it. That "
                "is a data gap, not a stock position of zero."
            ),
        )

    result = suggest(metric, _decimal(quantity, "quantity"), build_quantity_config())

    return QuantitySuggestionResponse(
        material=result.material,
        plant=result.plant,
        requested_quantity=str(result.requested_quantity),
        suggested_quantity=(
            None if result.suggested_quantity is None else str(result.suggested_quantity)
        ),
        available=result.available,
        is_override=result.is_override,
        reason=result.reason,
        no_suggestion_reason=(
            result.no_suggestion_reason.value if result.no_suggestion_reason else None
        ),
        stock_on_hand=(
            None if result.stock_on_hand is None else str(result.stock_on_hand)
        ),
        open_po_quantity=str(result.open_po_quantity),
        average_monthly_consumption=str(result.average_monthly_consumption),
        months_of_cover=(
            None if result.months_of_cover is None else str(result.months_of_cover)
        ),
        projected_cover_if_suggested=(
            None
            if result.projected_cover_if_suggested is None
            else str(result.projected_cover_if_suggested)
        ),
        projected_cover_if_requested=(
            None
            if result.projected_cover_if_requested is None
            else str(result.projected_cover_if_requested)
        ),
        consumption_count=result.consumption_count,
        cover_ceiling_months=str(result.config.cover_ceiling_months),
        lookback_months=result.config.lookback_months,
        min_history_consumptions=result.config.min_history_consumptions,
        basis_note=(
            "The cover ceiling, look-back window and minimum history are OUR "
            "defaults -- the FRS names all three as configuration and gives "
            "numbers for none of them. They are served with every suggestion so "
            "the number can be argued with rather than taken."
        ),
    )

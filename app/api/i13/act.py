"""W6.6 ACT API: read-only utilisation/aging over the W6.3 mart, the ACT
exception queue, requester confirmation, exception history, and the two
application operations (``detect_exceptions``/``process_escalations``) as
manual/local trigger endpoints -- no scheduler exists in this codebase yet
(see ``app.initiatives.i13.act.service``'s docstring), so this is how a
human or a script runs them today; a future Azure scheduler/job would call
the same underlying functions, not reinvent them.

Routes stay thin: every computation lives in ``app.initiatives.i13.act`` and
``app.initiatives.i13.watch_mart``. Mounted under ``/api/i13/act`` -- kept
separate from the existing ``/api/i13/watch`` (which still live-computes,
unchanged) and ``/api/i13/exceptions`` (W6.3's older, ephemeral, in-memory
exception queue) so neither pre-existing route's behaviour changes.
"""

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.i13.deps import Actor, get_current_actor, get_data_dir
from app.core.db import get_db
from app.initiatives.i13.act.domain import ExceptionStatus, ExceptionType
from app.initiatives.i13.act.service import (
    detect_exceptions,
    get_cross_plant_stock,
    process_escalations,
    submit_confirmation,
)
from app.initiatives.i13.act.state_machine import InvalidTransitionError
from app.initiatives.i13.act_exception_store import SqlExceptionRepository
from app.initiatives.i13.act_hod_provider import ConfigEscalationRecipientProvider
from app.initiatives.i13.act_notifications import LoggingNotificationAdapter
from app.initiatives.i13.act_stock_provider import PostgresCrossPlantStockProvider
from app.initiatives.i13.act_watch_snapshot import build_grni_snapshot_index
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.consumption_attribution import ConsumptionAttributionService
from app.initiatives.i13.plans import load_consumption_plans
from app.initiatives.i13.quantity_suggestion_store import build_quantity_decision_records
from app.initiatives.i13.reservation_ledger import build_reservation_ledger
from app.initiatives.i13.watch_mart import get_watch_metric, list_watch_metrics
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.schemas.i13 import WatchMetricResponse
from app.schemas.i13_act import (
    ActExceptionDetailResponse,
    ActExceptionEventResponse,
    ActExceptionResponse,
    CrossPlantStockResponse,
    DetectionRunRequest,
    DetectionRunResponse,
    EscalationRunRequest,
    EscalationRunResponse,
    RequesterConfirmationRequest,
    RequesterConfirmationResponse,
)

router = APIRouter(prefix="/act", tags=["i13-act"])


def _repository(db: Session) -> SqlExceptionRepository:
    return SqlExceptionRepository(db)


def _parse_enum(enum_cls, raw: str | None, field_name: str):
    if raw is None:
        return None
    try:
        return enum_cls(raw.upper())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid {field_name}: {raw!r}") from None


# --- Utilisation / aging (W6.3 mart, read-only) ---


@router.get("/utilisation", response_model=list[WatchMetricResponse])
def list_act_utilisation(
    plant: str | None = Query(None),
    material: str | None = Query(None),
    aging_band: str | None = Query(None),
    grni: bool | None = Query(None, description="Filter by W6.3's gr_not_issued_flag"),
    acquired_vs_plan_status: str | None = Query(None),
    limit: int = Query(1000, ge=1, le=5000, description="See the note on pagination below."),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[WatchMetricResponse]:
    """Read-only over the persisted W6.3 mart -- never recalculates months
    of cover, aging, GRNI or acquired-vs-plan (see
    ``app.initiatives.i13.watch_mart.list_watch_metrics``).

    ``limit``/``offset`` were added after the mart reached 7,184 rows against
    an OAR population of 44,394: the route had been returning the whole
    filtered set in one response, and the dashboard had been rendering it. The
    default is generous rather than small (1,000) so no existing caller loses
    data it was relying on, and the frontend discloses on screen when a
    response was capped -- an undisclosed cap reads as "that is all there is".
    """
    rows = list_watch_metrics(
        db,
        plant=plant,
        material=material,
        aging_band=aging_band,
        gr_not_issued_flag=grni,
        acquired_vs_plan_status=acquired_vs_plan_status,
    )
    page = rows[offset : offset + limit]
    return [WatchMetricResponse.model_validate(row) for row in page]


@router.get("/utilisation/{material}/{plant}", response_model=WatchMetricResponse)
def get_act_utilisation(material: str, plant: str, db: Session = Depends(get_db)) -> WatchMetricResponse:
    row = get_watch_metric(db, material, plant)
    if row is None:
        raise HTTPException(status_code=404, detail="no WATCH mart row for this material/plant yet -- run the W6.3 refresh first")
    return WatchMetricResponse.model_validate(row)


# --- ACT exceptions ---


@router.get("/exceptions", response_model=list[ActExceptionResponse])
def list_act_exceptions(
    plant: str | None = Query(None),
    material: str | None = Query(None),
    exception_type: str | None = Query(None, alias="type"),
    exception_status: str | None = Query(None, alias="status"),
    owner_requester_id: str | None = Query(None),
    limit: int = Query(1000, ge=1, le=5000, description="See the note on pagination below."),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[ActExceptionResponse]:
    """The FR-9 exception queue.

    ``limit``/``offset`` matter more here than anywhere else in I13: detection
    has raised 42,649 exceptions, and this route had no bound at all -- one
    unfiltered call serialised every one of them. The default of 1,000 is a
    readable screenful rather than a small page, and the frontend says when a
    response was capped instead of presenting a truncated queue as the queue.
    """
    repository = _repository(db)
    items = repository.list(
        material=material,
        plant=plant,
        exception_type=_parse_enum(ExceptionType, exception_type, "type"),
        status=_parse_enum(ExceptionStatus, exception_status, "status"),
        owner_requester_id=owner_requester_id,
    )
    page = items[offset : offset + limit]
    return [ActExceptionResponse.model_validate(item) for item in page]


@router.get("/exceptions/{exception_id}", response_model=ActExceptionDetailResponse)
def get_act_exception(exception_id: str, db: Session = Depends(get_db)) -> ActExceptionDetailResponse:
    repository = _repository(db)
    exception = repository.get(exception_id)
    if exception is None:
        raise HTTPException(status_code=404, detail="ACT exception not found")

    stock_provider = PostgresCrossPlantStockProvider(PostgresMovementRepository(db))
    cross_plant = get_cross_plant_stock(exception, stock_provider)
    confirmation = repository.get_confirmation(exception_id)

    return ActExceptionDetailResponse(
        **ActExceptionResponse.model_validate(exception).model_dump(),
        cross_plant_stock=[CrossPlantStockResponse.model_validate(item) for item in cross_plant],
        confirmation=RequesterConfirmationResponse.model_validate(confirmation) if confirmation else None,
    )


@router.post("/exceptions/{exception_id}/confirmation", response_model=ActExceptionResponse)
def confirm_act_exception(
    exception_id: str,
    payload: RequesterConfirmationRequest,
    actor: Actor = Depends(get_current_actor),
    db: Session = Depends(get_db),
) -> ActExceptionResponse:
    """The only way a requester confirmation/justification is recorded.
    Every field is structured and validated here; there is no endpoint that
    lets a client set ``status`` directly -- state only ever changes through
    ``app.initiatives.i13.act.service``'s state-machine-validated transitions."""
    repository = _repository(db)
    try:
        updated = submit_confirmation(
            exception_id=exception_id,
            reason_category=payload.reason_category,
            free_text=payload.free_text,
            actor_id=actor.id,
            as_of_time=datetime.now(timezone.utc),
            repository=repository,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="ACT exception not found") from None
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    return ActExceptionResponse.model_validate(updated)


@router.get("/exceptions/{exception_id}/history", response_model=list[ActExceptionEventResponse])
def get_act_exception_history(exception_id: str, db: Session = Depends(get_db)) -> list[ActExceptionEventResponse]:
    repository = _repository(db)
    if repository.get(exception_id) is None:
        raise HTTPException(status_code=404, detail="ACT exception not found")
    events = repository.list_events(exception_id)
    return [ActExceptionEventResponse.model_validate(event) for event in events]


# --- Detection / escalation (callable manually/locally today; a future
# Azure scheduler invokes the same two operations, unchanged) ---


@router.post("/run/detect", response_model=DetectionRunResponse)
def run_detect_exceptions(
    payload: DetectionRunRequest | None = None,
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
    data_dir: Path = Depends(get_data_dir),
) -> DetectionRunResponse:
    payload = payload or DetectionRunRequest()
    as_of_time = payload.as_of_time or datetime.now(timezone.utc)

    procurement_repo = PostgresProcurementRepository(db)
    reservation_repo = PostgresReservationRepository(db)
    material_scope_index = fetch_material_scope_index(db, material=payload.material, plant=payload.plant)

    ledger_entries = build_reservation_ledger(
        reservation_repo,
        procurement_repo,
        material_scope_index=material_scope_index,
        material=payload.material,
        plant=payload.plant,
        include_out_of_scope=True,
    )
    # `db` passed so detection sees plans CAPTURED through the assistant, not
    # only the 742 fabricated rows in the CSV. This is the join the whole of
    # WS7 exists to make: the first real plan is the first time detection has
    # ever seen data it did not author.
    plans = load_consumption_plans(data_dir, db)
    if payload.material:
        plans = [plan for plan in plans if plan.material == payload.material]
    if payload.plant:
        plans = [plan for plan in plans if plan.plant == payload.plant]

    grni_snapshots = build_grni_snapshot_index(list_watch_metrics(db, plant=payload.plant, material=payload.material))

    # W7.4 is the quantity-suggestion source W6.6's QUANTITY_OVERRIDE rule was
    # written against and then left idle ("suggested_quantity is None for every
    # caller today"). These are the decided suggestions -- accepted or not --
    # so the rule now has something to compare; before W7.4 this list was
    # necessarily empty. See quantity_suggestion_store for why undecided
    # suggestions are excluded.
    quantity_decision_records = build_quantity_decision_records(db, material=payload.material, plant=payload.plant)

    # W6.4's requester, so a NO_PLAN exception has somebody to route to.
    #
    # Without this the owner came from `ConsumptionPlan.requester` alone, and a
    # NO_PLAN exception has no plan by definition -- so every one of them was
    # unowned and FR-9's routing and escalation never fired for any of them.
    # W6.4 resolves a requester from RESB.WEMPF for roughly four reservations in
    # five and has always been sitting right here, unread.
    #
    # `reservation_repo` is the same instance build_reservation_ledger just
    # used, and its reads are memoized per instance, so this is a cache hit
    # rather than a second pass over the reservations.
    attribution_service = ConsumptionAttributionService(
        cost_centre_enabled=config.attribution.cost_centre_enabled
    )
    attributions = attribution_service.attribute_entries(
        ledger_entries,
        reservation_repo.get_reservations(material=payload.material, plant=payload.plant),
        plans,
    )
    # Only the resolved ones. An AMBIGUOUS attribution yields requester_id=None
    # and is left out entirely, so a conflict leaves the exception unowned
    # rather than routing it to whichever name sorted first.
    requester_by_reservation = {
        (a.reservation_number, a.reservation_item): a.requester_id
        for a in attributions
        if a.requester_id
    }

    result = detect_exceptions(
        as_of_time,
        ledger_entries=ledger_entries,
        plans=plans,
        grni_snapshots=grni_snapshots,
        repository=_repository(db),
        notification_port=LoggingNotificationAdapter(),
        plan_breach_grace_days=config.exceptions.plan_breach_grace_days,
        requester_response_days=config.escalation.requester_response_days,
        quantity_decision_records=quantity_decision_records,
        requester_by_reservation=requester_by_reservation,
    )
    db.commit()
    return DetectionRunResponse(**dataclasses.asdict(result))


@router.post("/run/escalate", response_model=EscalationRunResponse)
def run_process_escalations(
    payload: EscalationRunRequest | None = None,
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
) -> EscalationRunResponse:
    payload = payload or EscalationRunRequest()
    as_of_time = payload.as_of_time or datetime.now(timezone.utc)

    result = process_escalations(
        as_of_time,
        repository=_repository(db),
        escalation_recipient_provider=ConfigEscalationRecipientProvider.from_config_string(config.escalation.hod_recipients_raw),
        notification_port=LoggingNotificationAdapter(),
    )
    db.commit()
    return EscalationRunResponse(**dataclasses.asdict(result))

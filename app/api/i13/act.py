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
from app.initiatives.i13.plans import load_consumption_plans
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
    db: Session = Depends(get_db),
) -> list[WatchMetricResponse]:
    """Read-only over the persisted W6.3 mart -- never recalculates months
    of cover, aging, GRNI or acquired-vs-plan (see
    ``app.initiatives.i13.watch_mart.list_watch_metrics``)."""
    rows = list_watch_metrics(
        db,
        plant=plant,
        material=material,
        aging_band=aging_band,
        gr_not_issued_flag=grni,
        acquired_vs_plan_status=acquired_vs_plan_status,
    )
    return [WatchMetricResponse.model_validate(row) for row in rows]


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
    db: Session = Depends(get_db),
) -> list[ActExceptionResponse]:
    repository = _repository(db)
    items = repository.list(
        material=material,
        plant=plant,
        exception_type=_parse_enum(ExceptionType, exception_type, "type"),
        status=_parse_enum(ExceptionStatus, exception_status, "status"),
        owner_requester_id=owner_requester_id,
    )
    return [ActExceptionResponse.model_validate(item) for item in items]


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
    plans = load_consumption_plans(data_dir)
    if payload.material:
        plans = [plan for plan in plans if plan.material == payload.material]
    if payload.plant:
        plans = [plan for plan in plans if plan.plant == payload.plant]

    grni_snapshots = build_grni_snapshot_index(list_watch_metrics(db, plant=payload.plant, material=payload.material))

    result = detect_exceptions(
        as_of_time,
        ledger_entries=ledger_entries,
        plans=plans,
        grni_snapshots=grni_snapshots,
        repository=_repository(db),
        notification_port=LoggingNotificationAdapter(),
        plan_breach_grace_days=config.exceptions.plan_breach_grace_days,
        requester_response_days=config.escalation.requester_response_days,
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

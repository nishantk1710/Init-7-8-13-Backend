"""W6.6 ACT API contracts.

Routes convert ``app.initiatives.i13.act.domain`` dataclasses to these
before returning -- the persisted ORM row shape
(``app.models.i13_act_exception``) never reaches the API layer, matching
the rest of Initiative 13's schema convention (see ``app/schemas/i13.py``).
Utilisation/aging reads reuse ``WatchMetricResponse`` unchanged (see
``app/api/i13/act.py``): the W6.3 mart's row shape already matches it
field-for-field, so no separate schema is needed.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.initiatives.i13.act.domain import AssigneeType, ExceptionStatus, ExceptionType, RoutingStatus


class ActExceptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    exception_id: str
    exception_type: ExceptionType
    status: ExceptionStatus

    material: str
    plant: str

    reservation_number: str | None
    reservation_item: str | None
    session_id: str | None
    ledger_entry_id: str | None

    owner_requester_id: str | None

    detected_at: datetime
    requester_due_at: datetime | None
    escalated_at: datetime | None
    resolved_at: datetime | None

    current_assignee_type: AssigneeType | None
    current_assignee_id: str | None
    routing_status: RoutingStatus | None

    reason: str
    evidence: dict[str, str]

    created_at: datetime | None
    updated_at: datetime | None


class ActExceptionEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str | None
    event_type: str
    from_status: ExceptionStatus | None
    to_status: ExceptionStatus | None
    actor_id: str | None
    actor_type: str
    timestamp: datetime
    metadata: dict[str, str]


class RequesterConfirmationRequest(BaseModel):
    reason_category: str = Field(..., min_length=1, max_length=60)
    free_text: str = Field(..., min_length=1, max_length=2000)


class RequesterConfirmationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    exception_id: str
    reason_category: str
    free_text: str
    actor_id: str
    submitted_at: datetime


class CrossPlantStockResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    material: str
    plant: str
    stock_on_hand: Decimal


class ActExceptionDetailResponse(ActExceptionResponse):
    cross_plant_stock: list[CrossPlantStockResponse] = []
    confirmation: RequesterConfirmationResponse | None = None


class DetectionRunRequest(BaseModel):
    """``as_of_time`` defaults to now (UTC) when omitted -- this request
    boundary is the one place W6.6 calls the wall clock; everything past it
    (``app.initiatives.i13.act.service.detect_exceptions``) takes
    ``as_of_time`` as a plain argument."""

    as_of_time: datetime | None = None
    material: str | None = None
    plant: str | None = None


class DetectionRunResponse(BaseModel):
    as_of_time: datetime
    created: int
    reused: int
    resolved: int
    routed: int


class EscalationRunRequest(BaseModel):
    as_of_time: datetime | None = None


class EscalationRunResponse(BaseModel):
    as_of_time: datetime
    escalated: int
    routing_pending: int

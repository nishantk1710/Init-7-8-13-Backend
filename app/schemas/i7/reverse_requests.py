"""Min-Max -> OAR conversion requests: API schemas.

Thin wrappers over ``app.initiatives.i7.reverse_approval`` -- the API never re-implements a role check, a
stage move or the comment-required rule; those belong to the shared approval state machine.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.initiatives.i7.recommendations.types import ApprovalAction, ApprovalRole
from app.initiatives.i7.reverse_approval import service
from app.models.i7_reverse import ReverseApprovalLedger, ReverseConversionRequest


class ReverseRequestCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    material: str = Field(min_length=1, max_length=40)
    plant: str = Field(min_length=1, max_length=8)
    comment: str | None = Field(default=None, max_length=1000)
    """Why the planner is asking. Optional."""


class ReverseRequestDecline(BaseModel):
    model_config = ConfigDict(frozen=True)

    material: str = Field(min_length=1, max_length=40)
    plant: str = Field(min_length=1, max_length=8)
    comment: str = Field(min_length=1, max_length=1000)
    """Why the planner will not convert it. Required, and recorded with the decision."""


class ReverseRequestAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    actor_role: ApprovalRole
    """The stage the caller acts as. It must be the pending one, and one the caller's token really holds."""
    action: ApprovalAction
    comment: str | None = Field(default=None, max_length=1000)
    """Required for REJECT, SEND_BACK and HOLD."""


class ReverseLedgerOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    actor_id: str
    actor_role: str
    action: str
    previous_status: str
    new_status: str
    comment: str | None
    timestamp: datetime

    @classmethod
    def of(cls, entry: ReverseApprovalLedger) -> "ReverseLedgerOut":
        return cls(
            id=entry.id,
            actor_id=entry.actor_id,
            actor_role=entry.actor_role,
            action=entry.action,
            previous_status=entry.previous_status,
            new_status=entry.new_status,
            comment=entry.comment,
            timestamp=entry.timestamp,
        )


class ReverseRequestOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    material: str
    plant: str
    status: str
    """PENDING_APPROVAL, SENT_BACK, HELD, REJECTED or SAP_EXECUTION_PENDING (fully approved; SAP is changed by hand)."""
    pending_role: str | None
    route: list[str]
    chain_index: int
    confidence: str | None
    reason: str
    detail: str
    anchor_month: date | None
    stock_value: Decimal | None
    submitted_by: str
    submit_comment: str | None
    submitted_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, request: ReverseConversionRequest) -> "ReverseRequestOut":
        return cls(
            id=request.id,
            material=request.sap_material_number,
            plant=request.sap_plant_code,
            status=request.status,
            pending_role=request.pending_role,
            route=[role.value for role in service.ROUTE],
            chain_index=request.chain_index,
            confidence=request.confidence,
            reason=request.reason,
            detail=request.detail,
            anchor_month=request.anchor_month,
            stock_value=request.stock_value,
            submitted_by=request.submitted_by,
            submit_comment=request.submit_comment,
            submitted_at=request.submitted_at,
            updated_at=request.updated_at,
        )


class ReverseRequestDetail(BaseModel):
    """A material-plant's newest request (if any) and what happened to it."""

    model_config = ConfigDict(frozen=True)

    current: ReverseRequestOut | None
    history: list[ReverseLedgerOut]


class ReverseRequestListItem(BaseModel):
    """One material-plant's newest conversion request, as the Action Center and the Pipeline list it."""

    model_config = ConfigDict(frozen=True)

    id: int
    material: str
    plant: str
    description: str | None
    status: str
    pending_role: str | None
    route: list[str]
    chain_index: int
    confidence: str | None
    stock_value: Decimal | None
    submitted_by: str
    submitted_at: datetime
    updated_at: datetime


class ReverseRequestStatusCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    count: int


class ReverseRequestList(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[ReverseRequestListItem]
    total: int
    """Requests matching the status filter, across every page."""
    page: int
    page_size: int
    counts: list[ReverseRequestStatusCount]
    """Newest requests per status over every status, not narrowed by the filter."""

"""Approval workflow API schemas.

Thin wrappers around ``app.initiatives.i7.recommendations.workflow`` /
``ledger`` -- the API never re-implements a role check, a stage transition or
the comment-required rule. It validates shape (a real action enum value, a
non-empty comment where the domain requires one is still enforced by
``workflow.apply_action`` itself, not duplicated here) and calls the existing
service.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.initiatives.i7.recommendations.types import ApprovalAction, ApprovalRole
from app.models.i7_recommendation import ApprovalLedgerEntry


class ApprovalActionRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    actor_id: str = Field(min_length=1)
    """Until an authentication mechanism exists (``app.core.security`` is a
    placeholder, see docs/i07_api.md), the caller supplies this explicitly.
    It is not treated as a trusted identity claim -- see the security section
    of the API doc."""

    actor_role: ApprovalRole
    action: ApprovalAction
    comment: str | None = None


class SubmitRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    actor_id: str = Field(min_length=1)


class WorkflowStateResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    recommendation_id: str
    status: str
    pending_role: str | None
    chain_index: int
    adjustment_count: int
    current_version: int


class ApprovalHistoryEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    recommendation_id: str
    recommendation_version: int
    actor_id: str
    actor_role: str
    action: str
    previous_status: str
    new_status: str
    comment: str | None
    timestamp: datetime

    @classmethod
    def from_model(cls, row: ApprovalLedgerEntry) -> "ApprovalHistoryEntry":
        return cls(
            recommendation_id=row.recommendation_id,
            recommendation_version=row.recommendation_version,
            actor_id=row.actor_id,
            actor_role=row.actor_role,
            action=row.action,
            previous_status=row.previous_status,
            new_status=row.new_status,
            comment=row.comment,
            timestamp=row.timestamp,
        )


class ApprovalHistoryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    recommendation_id: str
    items: list[ApprovalHistoryEntry]

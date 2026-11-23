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

    actor_id: str | None = None
    """**Ignored.** Kept so older clients that still send it are not rejected. The actor recorded
    in the ledger -- and used to choose who is notified -- is the authenticated caller (the
    Entra object id from the token; the ``X-Actor-Id`` header only while sign-in is off), taken
    by the route, never from the body. ``actor_role`` below is still the caller's own claim:
    nothing checks it against the token's roles yet (see ``app/api/i7/approvals.py``)."""

    actor_role: ApprovalRole
    action: ApprovalAction
    comment: str | None = None
    new_rop: int | None = Field(default=None, ge=0)
    new_safety_stock: int | None = Field(default=None, ge=0)
    new_max_stock: int | None = Field(default=None, ge=0)
    """The values an ``ADJUST`` puts in place of the recommended ones: ``new_rop`` and ``new_max_stock`` are
    required for ADJUST (ignored by every other action), ``new_safety_stock`` is optional. Whole numbers, zero or
    more, with ROP <= max, and safety stock <= ROP when a safety stock is given (checked in
    ``ledger.apply_approval_action``). Left out, the recommended safety stock is not touched -- never set to 0."""


class SubmitRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    actor_id: str | None = None
    """**Ignored** -- the actor is the authenticated caller. See ``ApprovalActionRequest.actor_id``."""


class RejectRequest(BaseModel):
    """Declining a recommendation from the Recommendations page, before it is submitted."""

    model_config = ConfigDict(frozen=True)

    actor_id: str | None = None
    """**Ignored** -- the actor is the authenticated caller. See ``ApprovalActionRequest.actor_id``."""
    comment: str | None = None
    """The reason. Required -- ``workflow.reject_unsubmitted`` enforces it, as it does for every
    REJECT -- so it is not duplicated as a schema rule here."""


class WorkflowStateResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    recommendation_id: str
    status: str
    pending_role: str | None
    route: list[str]
    """The full approval route this recommendation follows, in order --
    OAR_APPROVAL_CHAIN for OAR conversion recommendations, or the
    criticality-routed ROP/Max chain otherwise (see
    ``recommendations.routing.route_for``)."""
    chain_index: int
    adjustment_count: int
    current_version: int


class ApprovalHistoryEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    recommendation_id: str
    recommendation_version: int
    actor_id: str
    actor_name: str | None = None
    """The actor's display name from ``app_user``; ``None`` when they have never signed in."""
    actor_role: str
    action: str
    previous_status: str
    new_status: str
    comment: str | None
    timestamp: datetime

    @classmethod
    def from_model(cls, row: ApprovalLedgerEntry, actor_name: str | None = None) -> "ApprovalHistoryEntry":
        return cls(
            recommendation_id=row.recommendation_id,
            recommendation_version=row.recommendation_version,
            actor_id=row.actor_id,
            actor_name=actor_name,
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


class ApprovalEventsResponse(BaseModel):
    """A page of the approval ledger across every recommendation, newest first.

    The portfolio-wide sibling of :class:`ApprovalHistoryResponse`: that one is
    one recommendation's complete history; this is the feed an audit trail
    reads. ``total`` is the whole filtered population, so a caller paging
    through it can say "12 of N" truthfully.
    """

    model_config = ConfigDict(frozen=True)

    items: list[ApprovalHistoryEntry]
    total: int
    limit: int
    offset: int

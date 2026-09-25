"""Approval ledger, read-only.

Returns the immutable audit trail exactly as written -- rows are never
updated or deleted by this or any other endpoint.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.i7.deps import get_session
from app.models.i7_recommendation import ApprovalLedgerEntry
from app.schemas.i7.approvals import ApprovalHistoryEntry, ApprovalHistoryResponse

router = APIRouter(tags=["i7-approvals"])


@router.get(
    "/recommendations/{recommendation_id}/approval-history",
    response_model=ApprovalHistoryResponse,
    summary="Get the approval ledger for a recommendation",
    description="The complete, append-only history of workflow actions -- "
    "never truncated or overwritten, and unaffected by the recommendation's "
    "own status moving on.",
)
def get_approval_history(
    recommendation_id: str, session: Annotated[Session, Depends(get_session)]
) -> ApprovalHistoryResponse:
    rows = session.execute(
        select(ApprovalLedgerEntry)
        .where(ApprovalLedgerEntry.recommendation_id == recommendation_id)
        .order_by(ApprovalLedgerEntry.timestamp, ApprovalLedgerEntry.id)
    ).scalars().all()

    return ApprovalHistoryResponse(
        recommendation_id=recommendation_id,
        items=[ApprovalHistoryEntry.from_model(row) for row in rows],
    )

"""The approval ledger across every recommendation, read-only.

``approval_history.py`` serves one recommendation's trail. The audit screen
needs the other shape -- what happened across the whole portfolio, newest
first, a page at a time -- which that route cannot give without being called
once per recommendation. This is that route, over the same append-only table
and nothing else: it neither writes nor joins, so a row shows exactly what was
recorded.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.i7.deps import get_session
from app.models.i7_recommendation import ApprovalLedgerEntry
from app.schemas.i7.approvals import ApprovalEventsResponse, ApprovalHistoryEntry

router = APIRouter(tags=["i7-approvals"])


@router.get(
    "/approval-events",
    response_model=ApprovalEventsResponse,
    summary="Approval ledger across all recommendations, newest first",
    description="A page of the append-only approval ledger, newest first, "
    "optionally bounded by `from_ts` (inclusive) and `to_ts` (exclusive). "
    "Filtering, ordering and paging happen in SQL; `total` is the whole "
    "filtered population, not the length of the page.",
)
def list_approval_events(
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    from_ts: Annotated[datetime | None, Query(description="Inclusive lower bound on timestamp.")] = None,
    to_ts: Annotated[datetime | None, Query(description="Exclusive upper bound on timestamp.")] = None,
) -> ApprovalEventsResponse:
    count_stmt = select(func.count()).select_from(ApprovalLedgerEntry)
    rows_stmt = select(ApprovalLedgerEntry)
    if from_ts is not None:
        count_stmt = count_stmt.where(ApprovalLedgerEntry.timestamp >= from_ts)
        rows_stmt = rows_stmt.where(ApprovalLedgerEntry.timestamp >= from_ts)
    if to_ts is not None:
        count_stmt = count_stmt.where(ApprovalLedgerEntry.timestamp < to_ts)
        rows_stmt = rows_stmt.where(ApprovalLedgerEntry.timestamp < to_ts)

    total = session.execute(count_stmt).scalar_one()
    # id breaks ties so two rows with the same timestamp page deterministically.
    rows = (
        session.execute(
            rows_stmt.order_by(ApprovalLedgerEntry.timestamp.desc(), ApprovalLedgerEntry.id.desc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )

    return ApprovalEventsResponse(
        items=[ApprovalHistoryEntry.from_model(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )

"""GET /api/i13/acquired-vs-plan and /acquired-vs-plan/counts.

The acquired-vs-plan tab: every captured plan and every OAR reservation raised
directly in SAP, with what was issued against it (``plan_vs_issued``). Served
from the stored snapshot's ``planvs`` rows, filtered and paged in SQL; the
in-memory snapshot and the live path compute the same rows.

Overdue is measured against the served version's reference date -- the date
every other I13 figure is measured as of -- so the marker and the filter agree
with the rest of the screen.
"""

from collections import Counter
from dataclasses import replace
from datetime import date

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.i13.deps import page, snapshot_or_live, with_material_names
from app.core.db import get_db
from app.initiatives.i13.ordering import newest_first
from app.initiatives.i13.plan_vs_issued import (
    PlanVsIssuedRow,
    RowSource,
    is_overdue,
    plan_vs_issued_rows,
)
from app.initiatives.i13.plans import load_captured_plans
from app.initiatives.i13.reservation_ledger import build_reservation_ledger
from app.initiatives.i13.session_link import session_by_reservation
from app.initiatives.i13.snapshot import I13Snapshot, reference_date_for
from app.initiatives.i13.snapshot_store.reader import SqlSnapshot
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.schemas.i13 import PlanVsIssuedCountsResponse, PlanVsIssuedRowResponse

router = APIRouter()

_SOURCE = Query(None, description="ASSISTANT (captured plans) or SAP (reservations raised directly).")
_STATUS = Query(None, description="NO_RESERVATION, NOT_DUE, ON_PLAN, BELOW_PLAN or ABOVE_PLAN.")
_RESERVATION = Query(None, description="One reservation number (exact match).")
_OVERDUE = Query(False, description="Only below-plan rows whose due date has passed.")


def _as_of(snapshot: I13Snapshot | SqlSnapshot | None) -> date:
    return snapshot.reference_date if snapshot is not None else reference_date_for()


def _computed_rows(
    db: Session, snapshot: I13Snapshot | None, *, material, plant, as_of: date
) -> list[PlanVsIssuedRow]:
    """The rows the stored snapshot would hold, computed here -- for the
    in-memory snapshot and ``?live=true``."""
    if snapshot is not None:
        ledger = list(snapshot.reservation_ledger)
    else:
        ledger = build_reservation_ledger(
            PostgresReservationRepository(db), PostgresProcurementRepository(db),
            material_scope_index=fetch_material_scope_index(db, material=material, plant=plant),
            material=material, plant=plant, include_out_of_scope=True,
        )
    rows = list(plan_vs_issued_rows(load_captured_plans(db), ledger, as_of=as_of))
    return [r for r in rows if (not material or r.material == material) and (not plant or r.plant == plant)]


def _matches(row: PlanVsIssuedRow, *, source, status, reservation, overdue=False, as_of: date | None = None) -> bool:
    return (
        (not source or row.source.value == source.upper())
        and (not status or row.status.value == status.upper())
        and (not reservation or any(r.split("/")[0] == reservation for r in row.reservations))
        and (not overdue or (as_of is not None and is_overdue(row, as_of)))
    )


def _with_session(row: PlanVsIssuedRow, sessions: dict[tuple[str, str], str]) -> PlanVsIssuedRow:
    """A SAP reservation's session, when its item text names one: looked up
    now, since links change between builds."""
    if row.source is not RowSource.SAP or not row.reservations:
        return row
    number, item = row.reservations[0].split("/")
    found = sessions.get((number, item))
    return replace(row, session_id=found) if found else row


def _response(row: PlanVsIssuedRow, sessions, as_of: date) -> PlanVsIssuedRowResponse:
    return PlanVsIssuedRowResponse.model_validate(_with_session(row, sessions)).model_copy(
        update={"overdue": is_overdue(row, as_of)}
    )


@router.get("/acquired-vs-plan", response_model=list[PlanVsIssuedRowResponse])
@with_material_names()
def list_plan_vs_issued(
    response: Response,
    source: str | None = _SOURCE,
    status: str | None = _STATUS,
    material: str | None = Query(None),
    plant: str | None = Query(None),
    reservation: str | None = _RESERVATION,
    overdue: bool = _OVERDUE,
    limit: int = Query(25, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    snapshot: I13Snapshot | SqlSnapshot | None = Depends(snapshot_or_live),
) -> list[PlanVsIssuedRowResponse]:
    as_of = _as_of(snapshot)
    if isinstance(snapshot, SqlSnapshot):
        rows, total = snapshot.plan_vs_issued(
            source=source, status=status, material=material, plant=plant, reservation=reservation,
            overdue=overdue, limit=limit, offset=offset,
        )
        response.headers["X-Total-Count"] = str(total)
    else:
        found = [
            r for r in _computed_rows(db, snapshot, material=material, plant=plant, as_of=as_of)
            if _matches(r, source=source, status=status, reservation=reservation, overdue=overdue, as_of=as_of)
        ]
        rows = list(page(newest_first(found, lambda r: r.due_date), response, limit=limit, offset=offset))
    sessions = session_by_reservation(db) if any(r.source is RowSource.SAP for r in rows) else {}
    return [_response(r, sessions, as_of) for r in rows]


@router.get("/acquired-vs-plan/counts", response_model=PlanVsIssuedCountsResponse)
def count_plan_vs_issued(
    source: str | None = _SOURCE,
    status: str | None = _STATUS,
    material: str | None = Query(None),
    plant: str | None = Query(None),
    reservation: str | None = _RESERVATION,
    overdue: bool = _OVERDUE,
    db: Session = Depends(get_db),
    snapshot: I13Snapshot | SqlSnapshot | None = Depends(snapshot_or_live),
) -> PlanVsIssuedCountsResponse:
    """The tab badge and the filters' counts, over every row -- not a page.
    ``by_source`` ignores the source filter, ``by_status`` the status one, and
    ``overdue`` the status and overdue ones."""
    if isinstance(snapshot, SqlSnapshot):
        return PlanVsIssuedCountsResponse(
            **snapshot.plan_vs_issued_counts(
                source=source, status=status, material=material, plant=plant, reservation=reservation,
                overdue=overdue,
            )
        )
    as_of = _as_of(snapshot)
    rows = [
        r for r in _computed_rows(db, snapshot, material=material, plant=plant, as_of=as_of)
        if _matches(r, source=None, status=None, reservation=reservation)
    ]

    def keep(r, **f) -> bool:
        return _matches(r, reservation=None, as_of=as_of, **f)

    return PlanVsIssuedCountsResponse(
        total=sum(1 for r in rows if keep(r, source=source, status=status, overdue=overdue)),
        by_source=dict(Counter(r.source.value for r in rows if keep(r, source=None, status=status, overdue=overdue))),
        by_status=dict(Counter(r.status.value for r in rows if keep(r, source=source, status=None, overdue=overdue))),
        overdue=sum(1 for r in rows if keep(r, source=source, status=None, overdue=True)),
    )

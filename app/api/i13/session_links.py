"""Session <-> reservation links (via RESB.SGTXT), the FR-4 compliance count,
and the UAT stand-in for SAP.

* ``GET /i13/session-links`` -- which reservation items carry which session ID.
* ``GET /i13/session-compliance`` -- OAR reservations required since go-live,
  by what their item text says (covered / session without plan / invalid
  session / missing session). The sessions screen's "reservations with no
  session" check, which could not be counted before SGTXT was readable.
* ``/i13/uat/*`` -- **only while ``I13_UAT_SIMULATION_ENABLED`` is on**
  (otherwise 404): simulate a reservation, stamp a session ID onto a real one,
  undo either. See ``app/initiatives/i13/uat.py``.

Reads and writes Postgres only; ``raw_resb`` is never modified.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.i13.deps import Actor, get_current_actor, page, require_snapshot
from app.core.config import get_settings
from app.core.db import get_db
from app.initiatives.i13 import uat as uat_service
from app.initiatives.i13.session_link import (
    COVERED,
    INVALID_SESSION,
    MISSING_SESSION,
    SESSION_WITHOUT_PLAN,
    go_live_date,
    load_sessions,
    session_by_reservation,
    session_status,
)
from app.initiatives.i13.snapshot import I13Snapshot
from app.models.i13_session_link import SessionReservationLink
from app.schemas.i13 import (
    SessionComplianceResponse,
    SessionLinkResponse,
    UatCandidateResponse,
    UatReservationResponse,
    UatSimulateRequest,
    UatStampRequest,
    UatStatusResponse,
)
from app.shared.material_scope import MaterialScope

router = APIRouter()


@router.get("/session-links", response_model=list[SessionLinkResponse])
def list_session_links(
    session_id: str | None = Query(None),
    material: str | None = Query(None),
    plant: str | None = Query(None),
    db: Session = Depends(get_db),
) -> list[SessionLinkResponse]:
    stmt = select(SessionReservationLink).order_by(SessionReservationLink.first_seen_at.desc())
    if session_id:
        stmt = stmt.where(SessionReservationLink.session_id == session_id.strip().upper())
    if material:
        stmt = stmt.where(SessionReservationLink.material == material)
    if plant:
        stmt = stmt.where(SessionReservationLink.plant == plant)
    return [SessionLinkResponse.model_validate(link) for link in db.execute(stmt).scalars()]


@router.get("/session-compliance", response_model=SessionComplianceResponse)
def get_session_compliance(
    plant: str | None = Query(None),
    db: Session = Depends(get_db),
    snapshot: I13Snapshot = Depends(require_snapshot),
) -> SessionComplianceResponse:
    """Counted over OAR reservations whose requirement date is on or after the
    go-live date -- the reservation carries no creation date in the extract, so
    the requirement date stands in for "made since the assistant existed"."""
    start = go_live_date(db)
    linked = session_by_reservation(db)
    sessions = load_sessions(db, set(linked.values()))
    counts = {COVERED: 0, SESSION_WITHOUT_PLAN: 0, INVALID_SESSION: 0, MISSING_SESSION: 0}
    seen: set[tuple[str, str]] = set()
    for entry in snapshot.reservation_ledger:
        key = (entry.reservation_number, entry.reservation_item)
        if key in seen or entry.material_scope is not MaterialScope.OAR:
            continue
        if plant and entry.plant != plant:
            continue
        if entry.requirement_date is None or entry.requirement_date < start:
            continue
        seen.add(key)
        counts[session_status(snapshot.sgtxt_by_reservation.get(key), linked.get(key), sessions)] += 1
    return SessionComplianceResponse(
        go_live_date=start,
        reservations=len(seen),
        covered=counts[COVERED],
        session_without_plan=counts[SESSION_WITHOUT_PLAN],
        invalid_session=counts[INVALID_SESSION],
        missing_session=counts[MISSING_SESSION],
    )


# --- UAT ---------------------------------------------------------------------


def _uat_enabled() -> None:
    if not get_settings().i13_uat_simulation_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="UAT simulation is not enabled")


@router.get("/uat", response_model=UatStatusResponse)
def get_uat_status(db: Session = Depends(get_db)) -> UatStatusResponse:
    return UatStatusResponse(enabled=get_settings().i13_uat_simulation_enabled, go_live_date=go_live_date(db))


@router.get("/uat/reservations", response_model=list[UatReservationResponse], dependencies=[Depends(_uat_enabled)])
def list_uat_reservations(
    session_id: str | None = Query(None), db: Session = Depends(get_db)
) -> list[UatReservationResponse]:
    return [UatReservationResponse.model_validate(r) for r in uat_service.list_rows(db, session_id=session_id)]


@router.get("/uat/candidates", response_model=list[UatCandidateResponse], dependencies=[Depends(_uat_enabled)])
def list_uat_candidates(
    response: Response,
    session_id: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    snapshot: I13Snapshot = Depends(require_snapshot),
) -> list[UatCandidateResponse]:
    """Existing reservations of the session's material and plant, latest
    requirement date first -- what a session ID could be stamped onto."""
    from app.assistant.models import AssistantSession

    session = db.get(AssistantSession, session_id.strip().upper())
    if session is None:
        raise HTTPException(status_code=404, detail=f"No assistant session {session_id!r}")
    linked = session_by_reservation(db)
    seen: set[tuple[str, str]] = set()
    rows: list[UatCandidateResponse] = []
    for entry in snapshot.reservation_ledger:
        key = (entry.reservation_number, entry.reservation_item)
        if (entry.material, entry.plant) != (session.material_id, session.plant) or key in seen:
            continue
        if entry.reservation_number.isdigit() and int(entry.reservation_number) >= uat_service.SIMULATED_RANGE_START:
            continue
        seen.add(key)
        rows.append(
            UatCandidateResponse(
                reservation_number=entry.reservation_number,
                reservation_item=entry.reservation_item,
                requirement_date=entry.requirement_date,
                reservation_quantity=entry.reservation_quantity,
                sgtxt=snapshot.sgtxt_by_reservation.get(key),
                session_id=linked.get(key),
                lifecycle_status=entry.lifecycle_status.value,
            )
        )
    rows.sort(key=lambda r: (r.requirement_date is not None, r.requirement_date), reverse=True)
    return list(page(rows, response, limit=limit))


def _uat_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except uat_service.UatConflict as conflict:
        raise HTTPException(status_code=409, detail=str(conflict)) from None
    except uat_service.UatError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None


@router.post(
    "/uat/reservations/simulate",
    response_model=UatReservationResponse,
    status_code=201,
    dependencies=[Depends(_uat_enabled)],
)
def simulate_uat_reservation(
    payload: UatSimulateRequest, actor: Actor = Depends(get_current_actor), db: Session = Depends(get_db)
) -> UatReservationResponse:
    """Stand in for the requester creating the reservation in SAP with the
    session ID in its item text. Numbered 99xxxxxx; no PR/PO/GR/GI."""
    row = _uat_call(uat_service.simulate, db, payload.session_id, actor=actor.id)
    return UatReservationResponse.model_validate(row)


@router.post(
    "/uat/reservations/stamp",
    response_model=UatReservationResponse,
    status_code=201,
    dependencies=[Depends(_uat_enabled)],
)
def stamp_uat_reservation(
    payload: UatStampRequest, actor: Actor = Depends(get_current_actor), db: Session = Depends(get_db)
) -> UatReservationResponse:
    """Stand in for the requester typing the session ID into an existing
    reservation's item text."""
    row = _uat_call(
        uat_service.stamp,
        db,
        payload.session_id,
        reservation_number=payload.reservation_number,
        reservation_item=payload.reservation_item,
        actor=actor.id,
    )
    return UatReservationResponse.model_validate(row)


@router.post("/uat/reservations/{uat_id}/remove", status_code=204, dependencies=[Depends(_uat_enabled)])
def remove_uat_reservation(uat_id: int, db: Session = Depends(get_db)) -> Response:
    """Undo a simulation or a stamp. A POST, not a DELETE: this API exposes no
    DELETE anywhere (see tests/test_write_paths.py), and a UAT tool is not a
    reason to start."""
    _uat_call(uat_service.remove, db, uat_id)
    return Response(status_code=204)

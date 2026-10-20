"""GET /api/i13/utilisation-ledger.

W6.2: Reservation -> PR -> PO -> GR -> GI, the complete I13 STITCH ledger,
OAR-scoped by default (W2.4). Reads real Postgres data only -- routes stay
thin; all stitching lives in ``app.initiatives.i13.reservation_ledger``.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.i13.deps import page, snapshot_or_live, with_expected_delivery, with_material_names
from app.core.db import get_db
from app.initiatives.i13.attribution import attribute_consumption
from app.initiatives.i13.models import LifecycleStatus, ReservationLedgerEntry
from app.initiatives.i13.ordering import newest_requirement_first
from app.initiatives.i13.reservation_ledger import build_reservation_ledger
from collections.abc import Mapping

from app.initiatives.i13.session_link import session_by_reservation
from app.initiatives.i13.snapshot import I13Snapshot
from app.initiatives.i13.snapshot_store.reader import SqlSnapshot
from app.initiatives.i13.uat import SIMULATED_RANGE_START
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.schemas.i13 import ReservationLedgerEntryResponse
from app.shared.material_scope import MaterialScope

router = APIRouter()


def _sgtxt_of(rows) -> dict[tuple[str, str], str]:
    return {(r["Rsnum"], r["Rspos"]): r["Sgtxt"] for r in rows if r.get("Sgtxt")}


def issued_before_history(entry: ReservationLedgerEntry, history_start: date | None) -> bool:
    """Nothing issued, and required before the earliest movement held: an issue
    could predate the history, so 0 is not a known zero. Pure, for the tests."""
    return bool(
        history_start
        and entry.requirement_date
        and entry.issued_quantity == 0
        and entry.requirement_date < history_start
    )


def _to_response(
    entry: ReservationLedgerEntry,
    sessions: dict[tuple[str, str], str] | None = None,
    sgtxt: Mapping[tuple[str, str], str] | None = None,
    history_start: date | None = None,
) -> ReservationLedgerEntryResponse:
    attribution = attribute_consumption(entry)
    response = ReservationLedgerEntryResponse.model_validate(entry)
    key = (entry.reservation_number, entry.reservation_item)
    return response.model_copy(
        update={
            "attribution_status": attribution.status,
            "attribution_evidence": attribution.evidence,
            "session_id": (sessions or {}).get(key),
            "sgtxt": (sgtxt or {}).get(key),
            "uat_simulated": int(entry.reservation_number) >= SIMULATED_RANGE_START
            if entry.reservation_number.isdigit()
            else False,
            "issued_before_history": issued_before_history(entry, history_start),
            "movement_history_start": history_start,
        }
    )


@router.get("/utilisation-ledger", response_model=list[ReservationLedgerEntryResponse])
@with_material_names()
@with_expected_delivery()
def list_reservation_ledger(
    response: Response,
    material: str | None = Query(None),
    plant: str | None = Query(None),
    reservation_number: str | None = Query(None),
    pr_number: str | None = Query(None),
    lifecycle_status: str | None = Query(None),
    include_out_of_scope: bool = Query(False, description="Include non-OAR (Min-Max/Excluded) materials."),
    session_id: str | None = Query(None, description="Only reservations whose SGTXT names this session."),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    snapshot: I13Snapshot | SqlSnapshot | None = Depends(snapshot_or_live),
) -> list[ReservationLedgerEntryResponse]:
    sessions = session_by_reservation(db)
    sgtxt: Mapping[tuple[str, str], str] | None = None
    if isinstance(snapshot, SqlSnapshot):
        try:
            wanted_status = LifecycleStatus(lifecycle_status.upper()).value if lifecycle_status else None
        except ValueError:
            wanted_status = ""  # matches nothing, as the in-memory filter does
        keys = None
        if session_id:
            wanted_session = session_id.strip().upper()
            keys = {key for key, sid in sessions.items() if sid == wanted_session}
        rows, total = snapshot.reservation_ledger(
            material=material, plant=plant, reservation_number=reservation_number, pr_number=pr_number,
            lifecycle_status=wanted_status, oar_only=not include_out_of_scope, reservation_keys=keys,
            limit=limit, offset=offset,
        )
        response.headers["X-Total-Count"] = str(total)
        texts = {(e.reservation_number, e.reservation_item): s for e, s in rows if s}
        return [_to_response(entry, sessions, texts, snapshot.recorded_history_start) for entry, _ in rows]
    if snapshot is not None:
        sgtxt = snapshot.sgtxt_by_reservation
        source = snapshot.reservation_ledger if include_out_of_scope else snapshot.reservation_ledger_oar
        entries = [
            e
            for e in source
            if (not material or e.material == material)
            and (not plant or e.plant == plant)
            and (not reservation_number or e.reservation_number == reservation_number)
            and (not pr_number or e.pr_number == pr_number)
        ]
    else:
        reservation_repo = PostgresReservationRepository(db)
        procurement_repo = PostgresProcurementRepository(db)
        material_scope_index = fetch_material_scope_index(db, material=material, plant=plant)
        entries = build_reservation_ledger(
            reservation_repo,
            procurement_repo,
            material_scope_index=material_scope_index,
            material=material,
            plant=plant,
            reservation_number=reservation_number,
            pr_number=pr_number,
            include_out_of_scope=include_out_of_scope,
        )
        # The same memoized rows the ledger was built from.
        sgtxt = _sgtxt_of(
            reservation_repo.get_reservations(
                reservation_number=reservation_number, pr_number=pr_number, material=material, plant=plant
            )
        )
    if lifecycle_status:
        try:
            wanted = LifecycleStatus(lifecycle_status.upper())
        except ValueError:
            wanted = None
        entries = [e for e in entries if e.lifecycle_status is wanted]
    if session_id:
        wanted = session_id.strip().upper()
        linked = {key for key, sid in sessions.items() if sid == wanted}
        entries = [e for e in entries if (e.reservation_number, e.reservation_item) in linked]
    # Newest requirement date first, as the stored snapshot orders it.
    entries = newest_requirement_first(entries)
    # The in-memory snapshot holds the same history start; the live path does not know it.
    history_start = snapshot.movement_history_start if snapshot is not None else None
    return [
        _to_response(entry, sessions, sgtxt, history_start)
        for entry in page(entries, response, limit=limit, offset=offset)
    ]


@router.get("/utilisation-ledger/{reservation_number}/{reservation_item}", response_model=ReservationLedgerEntryResponse)
@with_expected_delivery()
def get_reservation_ledger_entry(
    reservation_number: str,
    reservation_item: str,
    include_out_of_scope: bool = Query(False, description="See /utilisation-ledger."),
    db: Session = Depends(get_db),
    snapshot: I13Snapshot | SqlSnapshot | None = Depends(snapshot_or_live),
) -> ReservationLedgerEntryResponse:
    if isinstance(snapshot, SqlSnapshot):
        found = snapshot.reservation_entries(
            reservation_number, reservation_item, oar_only=not include_out_of_scope
        )
        if not found:
            raise HTTPException(status_code=404, detail="Reservation ledger entry not found")
        entry, text = found[0]
        key = (entry.reservation_number, entry.reservation_item)
        return _to_response(
            entry, session_by_reservation(db), {key: text} if text else {}, snapshot.recorded_history_start
        )
    if snapshot is not None:
        matching = [
            e
            for e in snapshot.reservation_ledger
            if e.reservation_number == reservation_number
            and e.reservation_item == reservation_item
            and (include_out_of_scope or e.material_scope is MaterialScope.OAR)
        ]
    else:
        reservation_repo = PostgresReservationRepository(db)
        procurement_repo = PostgresProcurementRepository(db)
        material_scope_index = fetch_material_scope_index(db)
        entries = build_reservation_ledger(
            reservation_repo,
            procurement_repo,
            material_scope_index=material_scope_index,
            reservation_number=reservation_number,
            include_out_of_scope=include_out_of_scope,
        )
        matching = [e for e in entries if e.reservation_item == reservation_item]
    if not matching:
        raise HTTPException(status_code=404, detail="Reservation ledger entry not found")
    sgtxt = (
        snapshot.sgtxt_by_reservation
        if snapshot is not None
        else _sgtxt_of(PostgresReservationRepository(db).get_reservations(reservation_number=reservation_number))
    )
    return _to_response(
        matching[0], session_by_reservation(db), sgtxt, snapshot.movement_history_start if snapshot is not None else None
    )

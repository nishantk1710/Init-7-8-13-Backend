"""GET /api/i13/utilisation-ledger.

W6.2: Reservation -> PR -> PO -> GR -> GI, the complete I13 STITCH ledger,
OAR-scoped by default (W2.4). Reads real Postgres data only -- routes stay
thin; all stitching lives in ``app.initiatives.i13.reservation_ledger``.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.initiatives.i13.models import LifecycleStatus
from app.initiatives.i13.reservation_ledger import build_reservation_ledger
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.schemas.i13 import ReservationLedgerEntryResponse

router = APIRouter()


@router.get("/utilisation-ledger", response_model=list[ReservationLedgerEntryResponse])
def list_reservation_ledger(
    material: str | None = Query(None),
    plant: str | None = Query(None),
    reservation_number: str | None = Query(None),
    pr_number: str | None = Query(None),
    lifecycle_status: str | None = Query(None),
    include_out_of_scope: bool = Query(False, description="Include non-OAR (Min-Max/Excluded) materials."),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[ReservationLedgerEntryResponse]:
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
    if lifecycle_status:
        try:
            wanted = LifecycleStatus(lifecycle_status.upper())
        except ValueError:
            wanted = None
        entries = [e for e in entries if e.lifecycle_status is wanted]
    page = entries[offset : offset + limit]
    return [ReservationLedgerEntryResponse.model_validate(entry) for entry in page]


@router.get("/utilisation-ledger/{reservation_number}/{reservation_item}", response_model=ReservationLedgerEntryResponse)
def get_reservation_ledger_entry(
    reservation_number: str,
    reservation_item: str,
    include_out_of_scope: bool = Query(False, description="See /utilisation-ledger."),
    db: Session = Depends(get_db),
) -> ReservationLedgerEntryResponse:
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
    return ReservationLedgerEntryResponse.model_validate(matching[0])

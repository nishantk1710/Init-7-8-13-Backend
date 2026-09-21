"""GET /api/i13/ledger, GET /api/i13/ledger/{ledger_id}.

Compatibility shim for the frontend contract that predates the Postgres
migration -- see ``app.initiatives.i13.ledger_compat`` for exactly what this
is built from now and why it isn't just an alias for the newer
``/utilisation-ledger`` endpoints (different grain, different fields).

W2.4 OAR scope is applied here, at the API boundary, matching the pre-
migration behaviour -- ``include_out_of_scope`` remains debug/inspection only.

Performance note the old contract never had to reckon with: the old CSV
dataset had ~8,900 PR rows total, so an unfiltered ``GET /ledger`` was cheap.
The real Postgres population is two orders of magnitude larger -- an
unfiltered, ``plant``-only call for a real plant returns on the order of
150k+ entries and takes 20+ seconds (measured against plant 1300). A
``material``+``plant`` call, the realistic per-material frontend case, is
sub-second. ``limit``/``offset`` (new, the old endpoint had neither) exist so
a client that forgets to filter by material gets a bounded, fast response
instead of a multi-second dump -- they do not fix the underlying full-table
build, which still runs before slicing. Filtering by ``material`` is the
actual fix; treat an unfiltered call as a diagnostic escape hatch, not a
page a UI should call routinely.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.initiatives.i13.ledger_compat import build_legacy_ledger
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.schemas.i13 import UtilisationLedgerEntryResponse

router = APIRouter()


@router.get("/ledger", response_model=list[UtilisationLedgerEntryResponse])
def list_ledger_entries(
    plant: str | None = Query(None),
    material: str | None = Query(None),
    include_out_of_scope: bool = Query(
        False, description="Include non-OAR (Min-Max/Excluded) materials. Debug/inspection only."
    ),
    limit: int = Query(100, ge=1, le=1000, description="Not in the original contract -- see module docstring."),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list[UtilisationLedgerEntryResponse]:
    procurement_repo = PostgresProcurementRepository(db)
    reservation_repo = PostgresReservationRepository(db)
    material_scope_index = fetch_material_scope_index(db, material=material, plant=plant)

    entries = build_legacy_ledger(
        procurement_repo, reservation_repo, material_scope_index,
        material=material, plant=plant, include_out_of_scope=include_out_of_scope,
    )
    page = entries[offset : offset + limit]
    return [UtilisationLedgerEntryResponse.model_validate(entry) for entry in page]


@router.get("/ledger/{ledger_id}", response_model=UtilisationLedgerEntryResponse)
def get_ledger_entry(
    ledger_id: str,
    include_out_of_scope: bool = Query(False, description="See /ledger."),
    db: Session = Depends(get_db),
) -> UtilisationLedgerEntryResponse:
    procurement_repo = PostgresProcurementRepository(db)
    reservation_repo = PostgresReservationRepository(db)
    material_scope_index = fetch_material_scope_index(db)

    entries = build_legacy_ledger(
        procurement_repo, reservation_repo, material_scope_index, include_out_of_scope=include_out_of_scope
    )
    for entry in entries:
        if entry.ledger_id == ledger_id:
            return UtilisationLedgerEntryResponse.model_validate(entry)
    raise HTTPException(status_code=404, detail="Ledger entry not found")

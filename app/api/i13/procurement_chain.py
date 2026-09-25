"""GET /api/i13/utilisation-ledger/partial, GET .../partial/diagnostics.

W6.1: PR -> PO -> GR -> GI, without the reservation leg. Reads real Postgres
data only (``PostgresProcurementRepository``) -- routes stay thin; all
stitching lives in ``app.initiatives.i13.procurement_chain``.
"""

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.i13.deps import page, snapshot_or_live
from app.core.db import get_db
from app.initiatives.i13.models import LifecycleStatus
from app.initiatives.i13.procurement_chain import build_procurement_chain, compute_chain_diagnostics
from app.initiatives.i13.snapshot import I13Snapshot
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.schemas.i13 import PartialLedgerEntryResponse, ProcurementChainDiagnosticsResponse

router = APIRouter()


@router.get("/utilisation-ledger/partial", response_model=list[PartialLedgerEntryResponse])
def list_partial_ledger(
    response: Response,
    material: str | None = Query(None),
    plant: str | None = Query(None),
    pr_number: str | None = Query(None),
    po_number: str | None = Query(None),
    lifecycle_status: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    snapshot: I13Snapshot | None = Depends(snapshot_or_live),
) -> list[PartialLedgerEntryResponse]:
    if snapshot is not None:
        entries = [
            e
            for e in snapshot.procurement_chain
            if (not material or e.material == material)
            and (not plant or e.plant == plant)
            and (not pr_number or e.pr_number == pr_number)
            and (not po_number or e.po_number == po_number)
        ]
    else:
        repository = PostgresProcurementRepository(db)
        entries = build_procurement_chain(
            repository, material=material, plant=plant, pr_number=pr_number, po_number=po_number
        )
    if lifecycle_status:
        try:
            wanted = LifecycleStatus(lifecycle_status.upper())
        except ValueError:
            wanted = None
        entries = [e for e in entries if e.lifecycle_status is wanted]
    return [PartialLedgerEntryResponse.model_validate(entry) for entry in page(entries, response, limit=limit, offset=offset)]


@router.get("/utilisation-ledger/partial/diagnostics", response_model=ProcurementChainDiagnosticsResponse)
def get_partial_ledger_diagnostics(
    material: str | None = Query(None),
    plant: str | None = Query(None),
    db: Session = Depends(get_db),
    snapshot: I13Snapshot | None = Depends(snapshot_or_live),
) -> ProcurementChainDiagnosticsResponse:
    # Only the tenant-wide diagnostics are precomputed; a filtered call is a
    # scoped query over EBAN/EKPO and stays live.
    if snapshot is not None and not material and not plant:
        return ProcurementChainDiagnosticsResponse.model_validate(snapshot.chain_diagnostics)
    repository = PostgresProcurementRepository(db)
    diagnostics = compute_chain_diagnostics(repository, material=material, plant=plant)
    return ProcurementChainDiagnosticsResponse.model_validate(diagnostics)

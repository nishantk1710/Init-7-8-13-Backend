"""GET /api/i13/ledger, GET /api/i13/ledger/{ledger_id}."""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.initiatives.i13.attribution import attribute_consumption
from app.initiatives.i13.ledger import build_utilisation_ledger
from app.initiatives.i13.models import UtilisationLedgerEntry
from app.integrations.sap.gateway import SapGateway, get_sap_gateway
from app.schemas.i13 import UtilisationLedgerEntryResponse

router = APIRouter()


def _to_response(entry: UtilisationLedgerEntry) -> UtilisationLedgerEntryResponse:
    attribution = attribute_consumption(entry)
    response = UtilisationLedgerEntryResponse.model_validate(entry)
    return response.model_copy(
        update={"attribution_status": attribution.status, "attribution_evidence": attribution.evidence}
    )


@router.get("/ledger", response_model=list[UtilisationLedgerEntryResponse])
def list_ledger_entries(
    plant: str | None = Query(None),
    material: str | None = Query(None),
    gateway: SapGateway = Depends(get_sap_gateway),
) -> list[UtilisationLedgerEntryResponse]:
    entries = build_utilisation_ledger(gateway)
    if plant:
        entries = [entry for entry in entries if entry.plant == plant]
    if material:
        entries = [entry for entry in entries if entry.material == material]
    return [_to_response(entry) for entry in entries]


@router.get("/ledger/{ledger_id}", response_model=UtilisationLedgerEntryResponse)
def get_ledger_entry(
    ledger_id: str, gateway: SapGateway = Depends(get_sap_gateway)
) -> UtilisationLedgerEntryResponse:
    entries = build_utilisation_ledger(gateway)
    for entry in entries:
        if entry.ledger_id == ledger_id:
            return _to_response(entry)
    raise HTTPException(status_code=404, detail="Ledger entry not found")

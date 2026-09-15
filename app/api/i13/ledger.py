"""GET /api/i13/ledger, GET /api/i13/ledger/{ledger_id}.

W2.4 OAR scope is applied here, at the I13 API boundary -- not inside
``build_utilisation_ledger`` itself, which stays a pure PR-> PO -> GR -> GI
stitcher that other I13 consumers (e.g. exceptions.py) also read from and
apply scope to on their own terms. ``include_out_of_scope`` exists only for
debugging/inspection; the default view is OAR-only, matching the FRS's
"I13 boundary" framing.
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from app.initiatives.i13.attribution import attribute_consumption
from app.initiatives.i13.ledger import build_utilisation_ledger
from app.initiatives.i13.models import UtilisationLedgerEntry
from app.integrations.sap.gateway import SapGateway, get_sap_gateway
from app.schemas.i13 import UtilisationLedgerEntryResponse
from app.shared.material_scope import MaterialScope, build_scope_index

router = APIRouter()


def _to_response(entry: UtilisationLedgerEntry) -> UtilisationLedgerEntryResponse:
    attribution = attribute_consumption(entry)
    response = UtilisationLedgerEntryResponse.model_validate(entry)
    return response.model_copy(
        update={"attribution_status": attribution.status, "attribution_evidence": attribution.evidence}
    )


def _in_oar_scope(
    entry: UtilisationLedgerEntry, scope_index: dict[tuple[str, str], MaterialScope]
) -> bool:
    return scope_index.get((entry.material, entry.plant)) is MaterialScope.OAR


@router.get("/ledger", response_model=list[UtilisationLedgerEntryResponse])
def list_ledger_entries(
    plant: str | None = Query(None),
    material: str | None = Query(None),
    include_out_of_scope: bool = Query(
        False, description="Include non-OAR (Min-Max/Excluded) materials. Debug/inspection only."
    ),
    gateway: SapGateway = Depends(get_sap_gateway),
) -> list[UtilisationLedgerEntryResponse]:
    entries = build_utilisation_ledger(gateway)
    if not include_out_of_scope:
        scope_index = build_scope_index(gateway.get_material_plants().rows)
        entries = [entry for entry in entries if _in_oar_scope(entry, scope_index)]
    if plant:
        entries = [entry for entry in entries if entry.plant == plant]
    if material:
        entries = [entry for entry in entries if entry.material == material]
    return [_to_response(entry) for entry in entries]


@router.get("/ledger/{ledger_id}", response_model=UtilisationLedgerEntryResponse)
def get_ledger_entry(
    ledger_id: str,
    include_out_of_scope: bool = Query(False, description="See /ledger."),
    gateway: SapGateway = Depends(get_sap_gateway),
) -> UtilisationLedgerEntryResponse:
    entries = build_utilisation_ledger(gateway)
    if not include_out_of_scope:
        scope_index = build_scope_index(gateway.get_material_plants().rows)
        entries = [entry for entry in entries if _in_oar_scope(entry, scope_index)]
    for entry in entries:
        if entry.ledger_id == ledger_id:
            return _to_response(entry)
    raise HTTPException(status_code=404, detail="Ledger entry not found")

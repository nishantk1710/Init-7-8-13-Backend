"""GET /api/i13/consumption-attribution.

W6.4: read-only verification route for consumption/ownership attribution.
Computes live off the W6.2 ledger, the same way ``watch.py``'s ``/watch``
does -- it never reads or writes the persisted mart
(``consumption_attribution_mart.py``), so hitting this endpoint has no side
effects. Routes stay thin -- all resolution logic lives in
``app.initiatives.i13.consumption_attribution``.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.i13.deps import get_data_dir, page, snapshot_or_live
from app.core.db import get_db
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.consumption_attribution import ConsumptionAttributionService
from app.initiatives.i13.models import ConsumptionAttribution
from app.initiatives.i13.plans import load_consumption_plans
from app.initiatives.i13.reservation_ledger import build_reservation_ledger
from app.initiatives.i13.snapshot import I13Snapshot
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.schemas.i13 import ConsumptionAttributionResponse
from app.shared.material_scope import MaterialScope

router = APIRouter()


def _from_snapshot(
    snapshot: I13Snapshot,
    *,
    material: str | None = None,
    plant: str | None = None,
    reservation_number: str | None = None,
    pr_number: str | None = None,
    include_out_of_scope: bool = False,
) -> list[ConsumptionAttribution]:
    """The snapshot's attributions, filtered the way ``_attribute`` filters its
    builds. ``pr_number`` is resolved through the snapshot's ledger, since an
    attribution record does not carry the PR."""
    ledger_ids = (
        {e.ledger_id for e in snapshot.reservation_ledger if e.pr_number == pr_number} if pr_number else None
    )
    return [
        a
        for a in snapshot.consumption_attribution
        if (not material or a.material == material)
        and (not plant or a.plant == plant)
        and (not reservation_number or a.reservation_number == reservation_number)
        and (ledger_ids is None or a.ledger_id in ledger_ids)
        and (include_out_of_scope or snapshot.scope_of(a.material, a.plant) is MaterialScope.OAR)
    ]


def _attribute(
    db: Session,
    config: I13Config,
    data_dir: Path,
    *,
    material: str | None = None,
    plant: str | None = None,
    reservation_number: str | None = None,
    pr_number: str | None = None,
    include_out_of_scope: bool = False,
) -> list[ConsumptionAttribution]:
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
    # Same repository instance/filters build_reservation_ledger just used --
    # memoized, so this is a cache hit, not a second query.
    reservation_rows = reservation_repo.get_reservations(
        reservation_number=reservation_number, pr_number=pr_number, material=material, plant=plant
    )
    plans = load_consumption_plans(data_dir, db)

    service = ConsumptionAttributionService(cost_centre_enabled=config.attribution.cost_centre_enabled)
    return service.attribute_entries(entries, reservation_rows, plans)


@router.get("/consumption-attribution", response_model=list[ConsumptionAttributionResponse])
def list_consumption_attribution(
    response: Response,
    material: str | None = Query(None),
    plant: str | None = Query(None),
    reservation_number: str | None = Query(None),
    pr_number: str | None = Query(None),
    include_out_of_scope: bool = Query(False, description="Include non-OAR (Min-Max/Excluded) materials."),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
    data_dir: Path = Depends(get_data_dir),
    snapshot: I13Snapshot | None = Depends(snapshot_or_live),
) -> list[ConsumptionAttributionResponse]:
    filters = dict(
        material=material,
        plant=plant,
        reservation_number=reservation_number,
        pr_number=pr_number,
        include_out_of_scope=include_out_of_scope,
    )
    attributions = _from_snapshot(snapshot, **filters) if snapshot is not None else _attribute(db, config, data_dir, **filters)
    return [
        ConsumptionAttributionResponse.model_validate(a)
        for a in page(attributions, response, limit=limit, offset=offset)
    ]


@router.get(
    "/consumption-attribution/{reservation_number}/{reservation_item}",
    response_model=ConsumptionAttributionResponse,
)
def get_consumption_attribution_entry(
    reservation_number: str,
    reservation_item: str,
    include_out_of_scope: bool = Query(False, description="See /consumption-attribution."),
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
    data_dir: Path = Depends(get_data_dir),
    snapshot: I13Snapshot | None = Depends(snapshot_or_live),
) -> ConsumptionAttributionResponse:
    filters = dict(reservation_number=reservation_number, include_out_of_scope=include_out_of_scope)
    attributions = _from_snapshot(snapshot, **filters) if snapshot is not None else _attribute(db, config, data_dir, **filters)
    matching = [a for a in attributions if a.reservation_item == reservation_item]
    if not matching:
        raise HTTPException(status_code=404, detail="Consumption attribution entry not found")
    return ConsumptionAttributionResponse.model_validate(matching[0])

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

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.i13.deps import page, snapshot_or_live, with_expected_delivery, with_material_names
from app.core.db import get_db
from app.core.logging import get_logger
from app.initiatives.i13.ledger_compat import LegacyLedgerEntry, build_legacy_ledger, delivery_state
from app.initiatives.i13.snapshot import I13Snapshot
from app.initiatives.i13.snapshot_store.reader import SqlSnapshot
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.schemas.i13 import LedgerCountsResponse, UtilisationLedgerEntryResponse
from app.shared.material_scope import MaterialScope

logger = get_logger(__name__)

router = APIRouter()

_STATUS_FILTERS = dict(
    procurement_status=Query(None, description="OPEN, PARTIALLY_RECEIVED, RECEIVED, CLOSED or NOT_AN_ORDER."),
    utilisation_status=Query(None, description="NOT_ISSUED, PARTIALLY_ISSUED or FULLY_ISSUED."),
    delivery_status=Query(None, description="DELIVERED, PARTIAL, NOT_DELIVERED, CLOSED, NOT_AN_ORDER or NO_PO."),
)


def _matching(
    entries: list[LegacyLedgerEntry],
    *,
    plant: str | None,
    material: str | None,
    procurement_status: str | None,
    utilisation_status: str | None,
    delivery_status: str | None,
) -> list[LegacyLedgerEntry]:
    """The in-memory and live paths' version of the stored filters."""
    wanted_p = procurement_status.upper() if procurement_status else None
    wanted_u = utilisation_status.upper() if utilisation_status else None
    wanted_d = delivery_status.upper() if delivery_status else None
    return [
        e
        for e in entries
        if (not plant or e.plant == plant)
        and (not material or e.material == material)
        and (wanted_p is None or e.procurement_status.value == wanted_p)
        and (wanted_u is None or e.utilisation_status.value == wanted_u)
        and (wanted_d is None or delivery_state(e) == wanted_d)
    ]


def _with_ordered_quantity(
    rows: list[UtilisationLedgerEntryResponse], snapshot: I13Snapshot | SqlSnapshot | None
) -> list[UtilisationLedgerEntryResponse]:
    """Fill ``ordered_quantity`` from the served snapshot: the stored
    version's PO work table, or the in-memory one's procurement chain. The
    live path (no snapshot) leaves it null.

    A screen still renders without it ("0 received" rather than "0 of 6"), so
    a failed read is logged and the page goes out as it is."""
    lines = [(r.po_number, r.po_item) for r in rows if r.po_number and r.po_item]
    if snapshot is None or not lines:
        return rows
    try:
        ordered = snapshot.ordered_quantities_for(lines)
    except Exception:  # noqa: BLE001 -- see the docstring
        logger.warning("I13 ledger: could not read ordered quantities", exc_info=True)
        return rows
    for row in rows:
        if row.po_number and row.po_item:
            row.ordered_quantity = ordered.get((row.po_number, row.po_item))
    return rows


def _unstored_entries(
    db: Session, snapshot: I13Snapshot | None, *, plant, material, include_out_of_scope
) -> list[LegacyLedgerEntry]:
    if snapshot is not None:
        return snapshot.legacy_ledger if include_out_of_scope else snapshot.legacy_ledger_oar
    procurement_repo = PostgresProcurementRepository(db)
    reservation_repo = PostgresReservationRepository(db)
    material_scope_index = fetch_material_scope_index(db, material=material, plant=plant)
    return build_legacy_ledger(
        procurement_repo, reservation_repo, material_scope_index,
        material=material, plant=plant, include_out_of_scope=include_out_of_scope,
        movement_repository=PostgresMovementRepository(db),
    )


@router.get("/ledger", response_model=list[UtilisationLedgerEntryResponse])
@with_material_names()
@with_expected_delivery()
def list_ledger_entries(
    response: Response,
    plant: str | None = Query(None),
    material: str | None = Query(None),
    procurement_status: str | None = _STATUS_FILTERS["procurement_status"],
    utilisation_status: str | None = _STATUS_FILTERS["utilisation_status"],
    delivery_status: str | None = _STATUS_FILTERS["delivery_status"],
    include_out_of_scope: bool = Query(
        False, description="Include non-OAR (Min-Max/Excluded) materials. Debug/inspection only."
    ),
    limit: int = Query(100, ge=1, le=1000, description="Not in the original contract -- see module docstring."),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    snapshot: I13Snapshot | SqlSnapshot | None = Depends(snapshot_or_live),
) -> list[UtilisationLedgerEntryResponse]:
    """Procurement lines, filtered and paged over the whole ledger. On the
    stored snapshot they come newest first (PR date, else first goods
    receipt); the in-memory and live paths keep build order."""
    filters = dict(
        procurement_status=procurement_status, utilisation_status=utilisation_status,
        delivery_status=delivery_status,
    )
    if isinstance(snapshot, SqlSnapshot):
        rows, total = snapshot.legacy_ledger(
            plant=plant, material=material, oar_only=not include_out_of_scope, limit=limit, offset=offset,
            **filters,
        )
        response.headers["X-Total-Count"] = str(total)
        return _with_ordered_quantity([UtilisationLedgerEntryResponse.model_validate(row) for row in rows], snapshot)
    entries = _matching(
        _unstored_entries(db, snapshot, plant=plant, material=material, include_out_of_scope=include_out_of_scope),
        plant=plant, material=material, **filters,
    )
    return _with_ordered_quantity(
        [
            UtilisationLedgerEntryResponse.model_validate(entry)
            for entry in page(entries, response, limit=limit, offset=offset)
        ],
        snapshot,
    )


@router.get("/ledger/counts", response_model=LedgerCountsResponse)
def count_ledger_entries(
    plant: str | None = Query(None),
    material: str | None = Query(None),
    procurement_status: str | None = _STATUS_FILTERS["procurement_status"],
    utilisation_status: str | None = _STATUS_FILTERS["utilisation_status"],
    delivery_status: str | None = _STATUS_FILTERS["delivery_status"],
    include_out_of_scope: bool = Query(False, description="See /ledger."),
    db: Session = Depends(get_db),
    snapshot: I13Snapshot | SqlSnapshot | None = Depends(snapshot_or_live),
) -> LedgerCountsResponse:
    """Lines in total and by linkage status, for the same filters as
    ``/ledger`` -- the header's numbers, without loading the lines."""
    filters = dict(
        procurement_status=procurement_status, utilisation_status=utilisation_status,
        delivery_status=delivery_status,
    )
    if isinstance(snapshot, SqlSnapshot):
        return LedgerCountsResponse(
            **snapshot.legacy_ledger_counts(
                plant=plant, material=material, oar_only=not include_out_of_scope, **filters
            )
        )
    entries = _matching(
        _unstored_entries(db, snapshot, plant=plant, material=material, include_out_of_scope=include_out_of_scope),
        plant=plant, material=material, **filters,
    )
    by_linkage = Counter(e.linkage_status.value for e in entries)
    return LedgerCountsResponse(total=len(entries), by_linkage_status=dict(by_linkage))


@router.get("/ledger/{ledger_id}", response_model=UtilisationLedgerEntryResponse)
@with_expected_delivery()
def get_ledger_entry(
    ledger_id: str,
    include_out_of_scope: bool = Query(False, description="See /ledger."),
    db: Session = Depends(get_db),
    snapshot: I13Snapshot | SqlSnapshot | None = Depends(snapshot_or_live),
) -> UtilisationLedgerEntryResponse:
    if isinstance(snapshot, SqlSnapshot):
        found = snapshot.legacy_entry(ledger_id, oar_only=not include_out_of_scope)
        if found is None:
            raise HTTPException(status_code=404, detail="Ledger entry not found")
        return _with_ordered_quantity([UtilisationLedgerEntryResponse.model_validate(found)], snapshot)[0]
    if snapshot is not None:
        entry = snapshot.legacy_ledger_by_id.get(ledger_id)
        if entry is not None and (
            include_out_of_scope or snapshot.scope_of(entry.material, entry.plant) is MaterialScope.OAR
        ):
            return _with_ordered_quantity([UtilisationLedgerEntryResponse.model_validate(entry)], snapshot)[0]
        raise HTTPException(status_code=404, detail="Ledger entry not found")

    procurement_repo = PostgresProcurementRepository(db)
    reservation_repo = PostgresReservationRepository(db)
    material_scope_index = fetch_material_scope_index(db)
    entries = build_legacy_ledger(
        procurement_repo, reservation_repo, material_scope_index, include_out_of_scope=include_out_of_scope,
        movement_repository=PostgresMovementRepository(db),
    )
    for entry in entries:
        if entry.ledger_id == ledger_id:
            return UtilisationLedgerEntryResponse.model_validate(entry)
    raise HTTPException(status_code=404, detail="Ledger entry not found")

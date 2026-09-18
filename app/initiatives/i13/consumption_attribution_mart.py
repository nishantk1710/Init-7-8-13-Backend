"""W6.4: persistence for consumption/ownership attribution
(``app.models.i13_consumption_attribution.ConsumptionAttributionRecord``).

Mirrors ``watch_mart.py``'s pattern exactly: refresh is a full, idempotent
recompute over the requested scope -- delete every row in that scope, then
insert one row per computed ``ConsumptionAttribution``, in a single unit of
work. Running it twice against unchanged source data leaves the same rows in
place, never duplicates. Portable delete-then-insert, not an ``ON CONFLICT``/
``MERGE`` upsert, for the same cross-dialect (Postgres/Azure SQL) reason
documented on ``ConsumptionAttributionRecord``.

Reuses W6.2's ``build_reservation_ledger`` unchanged -- this module never
rebuilds the Reservation -> PR -> PO -> GR -> GI chain, only attaches
ownership context on top (``consumption_attribution.py``), reading the same
raw reservation rows W6.2 already fetched plus the platform
``consumption_plans.csv`` (``plans.py``, same source ``exceptions.py``
already reads via ``data_dir``).
"""

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.initiatives.i13.config import I13Config
from app.initiatives.i13.consumption_attribution import ConsumptionAttributionService
from app.initiatives.i13.cost_centre_provider import CostCentreProvider
from app.initiatives.i13.models import ConsumptionAttribution
from app.initiatives.i13.plans import load_consumption_plans
from app.initiatives.i13.reservation_ledger import build_reservation_ledger
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.models.i13_consumption_attribution import ConsumptionAttributionRecord


def _to_row(attribution: ConsumptionAttribution, *, refreshed_at: datetime) -> ConsumptionAttributionRecord:
    return ConsumptionAttributionRecord(
        ledger_id=attribution.ledger_id,
        material=attribution.material,
        plant=attribution.plant,
        reservation_number=attribution.reservation_number,
        reservation_item=attribution.reservation_item,
        requester_id=attribution.requester_id,
        order_number=attribution.order_number,
        cost_centre=attribution.cost_centre,
        attribution_status=attribution.status.value,
        attribution_source=attribution.source.value,
        evidence=attribution.evidence,
        cost_centre_attribution_enabled=attribution.cost_centre_attribution_enabled,
        refreshed_at=refreshed_at,
    )


def refresh_consumption_attribution(
    session: Session,
    reservation_repository: PostgresReservationRepository,
    procurement_repository: PostgresProcurementRepository,
    material_scope_index: dict[tuple[str, str], str | None],
    config: I13Config,
    data_dir: Path,
    *,
    material: str | None = None,
    plant: str | None = None,
    reservation_number: str | None = None,
    pr_number: str | None = None,
    include_out_of_scope: bool = False,
    cost_centre_provider: CostCentreProvider | None = None,
) -> list[ConsumptionAttribution]:
    """Recompute W6.4 attribution for the W6.2 ledger entries in scope and
    replace their rows in the mart.

    The caller owns the transaction boundary (this codebase's convention --
    see ``watch_mart.py``'s docstring): this function does not call
    ``session.commit()``.
    """
    refreshed_at = datetime.now(timezone.utc)

    entries = build_reservation_ledger(
        reservation_repository,
        procurement_repository,
        material_scope_index=material_scope_index,
        material=material,
        plant=plant,
        reservation_number=reservation_number,
        pr_number=pr_number,
        include_out_of_scope=include_out_of_scope,
    )

    # Same repository instance, same filters as build_reservation_ledger just
    # used internally -- get_reservations is memoized per instance, so this
    # is a cache hit, not a second query (see postgres_reservation.py).
    reservation_rows = reservation_repository.get_reservations(
        reservation_number=reservation_number, pr_number=pr_number, material=material, plant=plant
    )

    plans = load_consumption_plans(data_dir)

    service = ConsumptionAttributionService(
        cost_centre_enabled=config.attribution.cost_centre_enabled,
        cost_centre_provider=cost_centre_provider,
    )
    attributions = service.attribute_entries(entries, reservation_rows, plans)

    delete_stmt = delete(ConsumptionAttributionRecord)
    if material:
        delete_stmt = delete_stmt.where(ConsumptionAttributionRecord.material == material)
    if plant:
        delete_stmt = delete_stmt.where(ConsumptionAttributionRecord.plant == plant)
    if reservation_number:
        delete_stmt = delete_stmt.where(ConsumptionAttributionRecord.reservation_number == reservation_number)
    session.execute(delete_stmt)

    for attribution in attributions:
        session.add(_to_row(attribution, refreshed_at=refreshed_at))
    session.flush()

    return attributions


def get_attribution_for_entry(session: Session, ledger_id: str) -> ConsumptionAttributionRecord | None:
    """Read-path counterpart to ``refresh_consumption_attribution`` -- the
    persisted attribution for one W6.2 ledger entry, or ``None`` if it
    hasn't been (re)computed yet."""
    return session.get(ConsumptionAttributionRecord, ledger_id)

"""W6.3: persistence for the WATCH utilisation mart
(``app.models.i13_watch_mart.WatchMetricMart``).

Refresh is a full, idempotent recompute -- delete every existing row, then
insert one row per computed ``WatchMetric``, in a single unit of work.
Running it twice against unchanged source data leaves the same rows in
place, never duplicates and never drifted totals: the FRS's idempotency
requirement. This is a portable delete-then-insert, not an ``ON CONFLICT``/
``MERGE`` upsert (``WatchMetricMart``'s docstring explains why: the target
is either Postgres or Azure SQL, and this repository keeps every model on
constructs both support).

Defaults to OAR-only (``material_scope is MaterialScope.OAR``) -- the FRS's
"W6.3 must operate on the OAR-scoped ledger coming from W6.2" -- reusing
W2.4/W6.2's existing ``classify_material_scope`` via ``WatchMetric.
material_scope`` (see ``watch.py``), never a second OAR rule computed here.
``compute_watch_metrics`` itself stays full-scope and unfiltered (unchanged
from before this module existed): ``exceptions.py`` and ``summary.py``
already depend on that breadth, and this module only adds a filtered,
persisted view on top -- the same "OAR scoping applied by callers that need
it" pattern ``watch.py``'s own docstring documents.
"""

from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.initiatives.i13.config import I13Config
from app.initiatives.i13.models import MaterialScope, WatchMartRefreshResult, WatchMetric
from app.initiatives.i13.watch import compute_watch_metrics
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.models.i13_watch_mart import WatchMetricMart


def _to_row(metric: WatchMetric, *, refreshed_at: datetime) -> WatchMetricMart:
    """One computed ``WatchMetric`` -> its persisted mart row. A pure
    field-for-field mapping -- no calculation happens here; see ``watch.py``
    for every formula. ``refreshed_at`` is set explicitly here (rather than
    left to the column's ``server_default``) so every row in one refresh
    carries the exact same timestamp and the caller can report it back
    without a round-trip read."""
    return WatchMetricMart(
        material=metric.material,
        plant=metric.plant,
        material_scope=metric.material_scope.value,
        stock_on_hand=metric.stock_on_hand,
        open_po_quantity=metric.open_po_quantity,
        average_monthly_consumption=metric.average_monthly_consumption,
        months_of_cover=metric.months_of_cover,
        projected_months_of_cover=metric.projected_months_of_cover,
        months_of_cover_reason=metric.months_of_cover_reason,
        last_movement_date=metric.last_movement_date,
        days_since_last_movement=metric.days_since_last_movement,
        last_issue_date=metric.last_issue_date,
        days_since_last_issue=metric.days_since_last_issue,
        consumption_count_12m=metric.consumption_count_12m,
        consumed_qty_12m=metric.consumed_qty_12m,
        inventory_turns=metric.inventory_turns,
        inventory_turns_reason=metric.inventory_turns_reason,
        aging_band=metric.aging_band.value,
        gr_not_issued_flag=metric.gr_not_issued_flag,
        gr_not_issued_days_since_gr=metric.gr_not_issued_days_since_gr,
        gr_not_issued_relevant_gr_date=metric.gr_not_issued_relevant_gr_date,
        gr_not_issued_threshold_days=metric.gr_not_issued_threshold_days,
        gr_not_issued_received_quantity=metric.gr_not_issued_received_quantity,
        gr_not_issued_issued_quantity=metric.gr_not_issued_issued_quantity,
        gr_not_issued_outstanding_quantity=metric.gr_not_issued_outstanding_quantity,
        acquired_vs_plan_status=metric.acquired_vs_plan_status.value,
        planned_quantity=metric.planned_quantity,
        received_quantity=metric.received_quantity,
        issued_quantity=metric.issued_quantity,
        acquired_vs_plan_variance_quantity=metric.acquired_vs_plan_variance_quantity,
        acquired_vs_plan_variance_percentage=metric.acquired_vs_plan_variance_percentage,
        calculated_at=metric.calculated_at,
        refreshed_at=refreshed_at,
    )


def refresh_watch_metrics_mart(
    session: Session,
    movement_repository: PostgresMovementRepository,
    procurement_repository: PostgresProcurementRepository,
    reservation_repository: PostgresReservationRepository,
    material_scope_index: dict[tuple[str, str], str | None],
    config: I13Config,
    data_dir: Path,
    *,
    material: str | None = None,
    plant: str | None = None,
    as_of: date | None = None,
    oar_only: bool = True,
) -> WatchMartRefreshResult:
    """Recompute WATCH metrics and replace the mart's rows with them.

    ``material``/``plant`` narrow the recompute the same way every other I13
    builder accepts them; omitted, this replaces every row currently in the
    mart with a fresh full-tenant recompute (unfiltered `DELETE` + reinsert
    only the OAR/full-scope rows just computed). A caller doing a partial
    (single material/plant) refresh is responsible for knowing that mixing
    partial and full refreshes leaves the mart holding whatever the two most
    recent calls, not this function's job -- there is no update-in-place
    merge to reason about instead.

    The caller owns the transaction boundary: this function does not call
    ``session.commit()`` (this codebase's own convention -- see
    ``app.core.db.get_db``'s docstring, "a route or service commits its own
    unit of work explicitly").
    """
    as_of = as_of or date.today()
    refreshed_at = datetime.now(timezone.utc)

    metrics = compute_watch_metrics(
        movement_repository,
        procurement_repository,
        reservation_repository,
        material_scope_index,
        config,
        data_dir,
        material=material,
        plant=plant,
        as_of=as_of,
    )
    if oar_only:
        metrics = [m for m in metrics if m.material_scope is MaterialScope.OAR]

    delete_stmt = delete(WatchMetricMart)
    if material:
        delete_stmt = delete_stmt.where(WatchMetricMart.material == material)
    if plant:
        delete_stmt = delete_stmt.where(WatchMetricMart.plant == plant)
    session.execute(delete_stmt)

    for metric in metrics:
        session.add(_to_row(metric, refreshed_at=refreshed_at))
    session.flush()

    return WatchMartRefreshResult(
        row_count=len(metrics),
        oar_only=oar_only,
        as_of=as_of,
        refreshed_at=refreshed_at,
    )

"""Recompute one material-plant inside the stored snapshot.

``snapshot.refresh_material``'s job for the SQL store: a plan captured through
the assistant gives that material's WATCH row something to measure against,
and a simulated or stamped UAT reservation changes its ledger. The material is
recomputed by the same builders, from the served version's own work tables
(kept for exactly this), and its rows are replaced in place -- the rest of the
version is untouched, so this costs about what one assessment costs.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.initiatives.i13.config import get_i13_config
from app.initiatives.i13.consumption_attribution import ConsumptionAttributionService
from app.initiatives.i13.exceptions import exception_queue_from
from app.initiatives.i13.plans import load_captured_plans, load_reference_plans
from app.initiatives.i13.reservation_ledger import build_reservation_ledger
from app.initiatives.i13.session_link import sync_links
from app.initiatives.i13.snapshot_store import kinds
from app.initiatives.i13.snapshot_store.batch_repos import (
    Batch,
    BatchMovementRepository,
    BatchProcurementRepository,
    BatchReservationRepository,
    scope_index,
)
from app.initiatives.i13.snapshot_store.builder import RecordWriter
from app.initiatives.i13.snapshot_store.lifecycle import ready_run
from app.initiatives.i13.snapshot_store.work_tables import WorkTables
from app.initiatives.i13.watch import compute_watch_metrics
from app.models.i13_snapshot_store import I13SnapshotRecord as R
from app.shared.material_scope import MaterialScope

logger = get_logger(__name__)


def refresh_material(db: Session, material: str, plant: str, *, reservations: bool = False) -> bool:
    """Replace one material-plant's rows in the served version. False when
    nothing is stored yet."""
    run = ready_run(db)
    if run is None:
        return False
    settings = get_settings()
    config = get_i13_config()
    data_dir = Path(settings.i13_data_dir)
    work = WorkTables(run.version)
    batch = Batch(material, material, plant)

    movement_repo = BatchMovementRepository(db, work, batch)
    procurement_repo = BatchProcurementRepository(db, work, batch)
    reservation_repo = BatchReservationRepository(db, work, batch)
    index = scope_index(db, work, batch)

    watch = [
        m
        for m in compute_watch_metrics(
            movement_repo, procurement_repo, reservation_repo, index, config, data_dir,
            material=material, plant=plant, as_of=run.reference_date, db=db,
        )
        if (m.material, m.plant) == (material, plant)
    ]

    replaced = [kinds.WATCH]
    records: list[kinds.Record] = [kinds.watch(m, m.material_scope is MaterialScope.OAR) for m in watch]

    if reservations:
        rows = reservation_repo.get_reservations(material=material, plant=plant)
        ledger = build_reservation_ledger(
            reservation_repo, procurement_repo, material_scope_index=index,
            material=material, plant=plant, include_out_of_scope=True,
        )
        reference_plans = load_reference_plans(data_dir)
        plans = list(reference_plans) + load_captured_plans(db)
        attribution = ConsumptionAttributionService(
            cost_centre_enabled=config.attribution.cost_centre_enabled
        ).attribute_entries(ledger, rows, plans)
        sgtxt = {(r["Rsnum"], r["Rspos"]): r["Sgtxt"] for r in rows if r.get("Sgtxt")}
        sync_links(db, rows, scope=(material, plant))

        oar = bool(watch) and watch[0].material_scope is MaterialScope.OAR
        pr_by_ledger = {e.ledger_id: e.pr_number for e in ledger}
        threshold = config.watch.gr_not_issued_threshold_days
        grni = []
        for entry in ledger:
            if entry.last_gr_date is None:
                continue
            outstanding = (entry.received_quantity or Decimal(0)) - entry.issued_quantity
            age = (run.reference_date - entry.last_gr_date).days
            if outstanding > 0 and age >= threshold:
                grni.append(kinds.grni(entry, outstanding, age))
        key_plans = [p for p in reference_plans if (p.material, p.plant) == (material, plant)]
        queue = exception_queue_from(ledger, key_plans, watch, config, as_of=run.reference_date)

        replaced += [kinds.RLEDGER, kinds.ATTRIB, kinds.GRNI, kinds.EXCEPTION]
        records += [kinds.rledger(e, sgtxt.get((e.reservation_number, e.reservation_item))) for e in ledger]
        records += [kinds.attrib(a, oar, pr_by_ledger.get(a.ledger_id)) for a in attribution]
        records += grni
        records += [kinds.exception(i, oar) for i in queue if (i.material, i.plant) == (material, plant)]

    db.execute(
        delete(R).where(R.version == run.version, R.kind.in_(replaced), R.material == material, R.plant == plant)
    )
    writer = RecordWriter(db, run.version)
    for kind in replaced:
        top = db.execute(select(func.max(R.seq)).where(R.version == run.version, R.kind == kind)).scalar()
        writer.seq[kind] = top or 0
    writer.write(records)
    db.flush()
    logger.info("I13 store: refreshed %s/%s in v%s (reservations=%s)", material, plant, run.version, reservations)
    return True

"""W6.6 detection, fed from the I13 snapshot when there is one.

``POST /api/i13/act/run/detect`` and the assistant's post-capture hook
(``app.assistant.turns``) both run detection through :func:`run_detection`, so
there is one way to gather its evidence:

* **ledger entries and GRNI evidence** come from the I13 snapshot's reservation
  ledger and WATCH rows. Before the snapshot, GRNI evidence came from
  ``i13_watch_metric_mart`` -- a table nothing in the app refreshed, holding
  7,184 of ~45k rows -- so most NO_PLAN_GRNI checks read "source unavailable".
  With no snapshot (``?live=true``, snapshot disabled) the ledger is rebuilt
  live exactly as the route always did, and GRNI falls back to that mart;
* **plans, quantity decisions, requesters** are read live, since people write
  them.

Scoped by material and/or plant, a run touches only exceptions for that scope
(``detect_exceptions`` upserts by deterministic id and never sweeps others),
which is what makes it cheap enough to run after every assistant capture.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.initiatives.i13.act.domain import WatchGrniSnapshot
from app.initiatives.i13.act.service import DetectionRunResult, detect_exceptions
from app.initiatives.i13.act_exception_store import SqlExceptionRepository
from app.initiatives.i13.act_notifications import LoggingNotificationAdapter
from app.initiatives.i13.act_watch_snapshot import build_grni_snapshot_index
from app.initiatives.i13.config import I13Config
from app.initiatives.i13.consumption_attribution import ConsumptionAttributionService
from app.initiatives.i13.plans import load_consumption_plans
from app.initiatives.i13.quantity_suggestion_store import (
    build_assistant_quantity_decision_records,
    build_quantity_decision_records,
)
from app.initiatives.i13.reservation_ledger import build_reservation_ledger
from app.initiatives.i13.snapshot import I13Snapshot, current_plans
from app.initiatives.i13.watch_mart import list_watch_metrics
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository


def _in_scope(material: str | None, plant: str | None):
    return lambda m, p: (not material or m == material) and (not plant or p == plant)


def run_detection(
    db: Session,
    config: I13Config,
    data_dir: Path,
    *,
    as_of_time: datetime,
    material: str | None = None,
    plant: str | None = None,
    snapshot: I13Snapshot | None = None,
) -> DetectionRunResult:
    """Gather detection's evidence and run it. The caller commits."""
    wanted = _in_scope(material, plant)
    reservation_repo = PostgresReservationRepository(db)

    if snapshot is not None:
        ledger_entries = [e for e in snapshot.reservation_ledger if wanted(e.material, e.plant)]
        plans = [p for p in current_plans(db, snapshot) if wanted(p.material, p.plant)]
        grni_snapshots = {
            key: WatchGrniSnapshot(
                material=m.material,
                plant=m.plant,
                gr_not_issued_flag=m.gr_not_issued_flag,
                gr_not_issued_days_since_gr=m.gr_not_issued_days_since_gr,
                gr_not_issued_threshold_days=m.gr_not_issued_threshold_days,
            )
            for key, m in snapshot.watch.items()
            if wanted(*key)
        }
        reservation_rows = reservation_repo.get_reservations(material=material, plant=plant)
    else:
        procurement_repo = PostgresProcurementRepository(db)
        material_scope_index = fetch_material_scope_index(db, material=material, plant=plant)
        ledger_entries = build_reservation_ledger(
            reservation_repo,
            procurement_repo,
            material_scope_index=material_scope_index,
            material=material,
            plant=plant,
            include_out_of_scope=True,
        )
        # `db` passed so detection sees plans CAPTURED through the assistant.
        plans = [p for p in load_consumption_plans(data_dir, db) if wanted(p.material, p.plant)]
        grni_snapshots = build_grni_snapshot_index(list_watch_metrics(db, plant=plant, material=material))
        # Same repository instance build_reservation_ledger just used: memoized.
        reservation_rows = reservation_repo.get_reservations(material=material, plant=plant)

    # Both quantity stores: the W7.4 API's and the assistant's (gap G5).
    quantity_decision_records = build_quantity_decision_records(
        db, material=material, plant=plant
    ) + build_assistant_quantity_decision_records(db, material=material, plant=plant)

    # W6.4's requester, so a NO_PLAN exception has somebody to route to. Only
    # resolved attributions: an AMBIGUOUS one leaves the exception unowned
    # rather than routing it to whichever name sorted first.
    attributions = ConsumptionAttributionService(
        cost_centre_enabled=config.attribution.cost_centre_enabled
    ).attribute_entries(ledger_entries, reservation_rows, plans)
    requester_by_reservation = {
        (a.reservation_number, a.reservation_item): a.requester_id for a in attributions if a.requester_id
    }

    return detect_exceptions(
        as_of_time,
        ledger_entries=ledger_entries,
        plans=plans,
        grni_snapshots=grni_snapshots,
        repository=SqlExceptionRepository(db),
        notification_port=LoggingNotificationAdapter(),
        plan_breach_grace_days=config.exceptions.plan_breach_grace_days,
        requester_response_days=config.escalation.requester_response_days,
        quantity_decision_records=quantity_decision_records,
        requester_by_reservation=requester_by_reservation,
    )

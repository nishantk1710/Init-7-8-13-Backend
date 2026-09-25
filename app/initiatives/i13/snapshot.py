"""The I13 in-memory snapshot: every read-heavy I13 result, computed once.

This is the serving pattern Initiative 8 already uses
(``app.initiatives.i8.service.get_snapshot``), applied to Initiative 13 -- see
``docs/I13_Instant_Load_Plan_24_Sep_2026.md`` §4.

Why
---
Every I13 read route used to rebuild its answer from the ``raw_*`` tables on
each request, in Python: ``/summary`` took over 90 seconds, ``/validation`` 17,
``/watch`` 12, ``/ledger`` 9. The builders themselves are not slow -- measured
on 24-Sep, the whole set runs in about 37 seconds when it shares one set of
repository instances (so every raw table is read once) -- it was paying that
cost per request, and paying the shared parts several times per request, that
made the screens unusable.

So the builders run once, here, into one immutable :class:`I13Snapshot`, and
the routes filter and page that object instead. The builders are unchanged:
the snapshot calls exactly the functions the routes used to call, so a
snapshot answer and a ``?live=true`` answer are the same computation.

What is *not* in the snapshot
-----------------------------
Anything a person writes: captured consumption plans, ACT exceptions and their
confirmations, assistant sessions, justifications, quantity suggestions. Those
are small tables and are read on every request, the way I8 reads attestations
live over its cached register. Results that depend on them -- the summary's
no-plan and plan-breach counts, the legacy exception queue -- are recomputed
from the snapshot plus the live plans at request time, cached only until the
plans change (:func:`exception_queue`).

A plan captured through the assistant also changes that material's WATCH row
(acquired-vs-plan). :func:`refresh_key` recomputes just that one row and swaps
it in, so the WATCH screen reflects the capture on the next view.

Lifecycle
---------
* **Start-up** (``app.main`` lifespan): :func:`start_background_build`. The
  server accepts requests immediately; snapshot-backed routes answer 503
  ``building`` until the first build lands.
* **Nothing started it** (a test, a script): the first :func:`get_i13_snapshot`
  builds synchronously -- the same lazy behaviour as I8's ``get_snapshot``.
* **A reseed or a new day**: the fingerprint (latest ``ingestion_run`` per I13
  table, the reference date, the I13 config, the reference-plans file) changes.
  :func:`check_fingerprint` notices and rebuilds in the background; the old
  snapshot keeps serving until the new one is swapped in.
* **Manual**: ``POST /api/i13/snapshot/refresh``.

One snapshot per process. Run the API with a single worker (decision D14).
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.consumption_attribution import ConsumptionAttributionService
from app.initiatives.i13.exceptions import exception_queue_from
from app.initiatives.i13.ledger_compat import LegacyLedgerEntry, build_legacy_ledger
from app.initiatives.i13.models import (
    AgingBand,
    ConsumptionAttribution,
    ExceptionQueueItem,
    MovementMetrics,
    PartialLedgerEntry,
    ProcurementChainDiagnostics,
    ReclassificationCandidate,
    ReservationLedgerEntry,
    WatchMetric,
)
from app.initiatives.i13.movement_metrics import compute_all_movement_metrics
from app.initiatives.i13.movements import ISSUE_TYPES, RECEIPT_TYPES, reversal_types_for
from app.initiatives.i13.plans import ConsumptionPlan, load_captured_plans, load_reference_plans
from app.initiatives.i13.procurement_chain import build_procurement_chain, compute_chain_diagnostics
from app.initiatives.i13.reclassification import build_reclassification_candidates
from app.initiatives.i13.reservation_ledger import build_reservation_ledger
from app.initiatives.i13.watch import compute_watch_metrics
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.models import IngestionRun
from app.shared.material_scope import MaterialScope, classify_material_scope

logger = get_logger(__name__)

Key = tuple[str, str]
Row = dict[str, Any]

#: The raw extract tables whose reload changes an I13 answer. ZMM065 is here
#: because reclassification reads criticality from it.
FINGERPRINT_TABLES = (
    "raw_eban",
    "raw_ekpo",
    "raw_ekbe",
    "raw_mseg",
    "raw_mkpf",
    "raw_mard",
    "raw_resb",
    "raw_marc",
    "raw_zmm065_bmm",
    "raw_zmm065_gb",
)


class SnapshotBuilding(Exception):
    """No snapshot is available yet because the first build is still running."""

    def __init__(self, started_at: datetime | None) -> None:
        super().__init__("The I13 snapshot is still being built")
        self.started_at = started_at


class SnapshotFailed(Exception):
    """No snapshot is available because the last build failed."""


@dataclass(frozen=True)
class MonthlyConsumption:
    """One material-plant-month of net goods movements.

    ``issued_quantity``/``issue_count`` use the same reversal-aware netting as
    WATCH's consumption (``movements.net_quantity``/``net_event_count`` over
    ``ISSUE_TYPES``); ``received_quantity`` the same over ``RECEIPT_TYPES``.
    """

    month: str  # "YYYY-MM"
    issued_quantity: Decimal
    issue_count: int
    received_quantity: Decimal


@dataclass(frozen=True)
class GrniEntry:
    """One reservation-ledger entry that has been received and not issued for
    at least the GR-not-issued threshold -- the per-entry form of WATCH's
    ``gr_not_issued_flag`` (``watch._gr_not_issued``), FRS FR-6."""

    ledger: ReservationLedgerEntry
    outstanding_quantity: Decimal
    days_since_gr: int


@dataclass(frozen=True)
class I13Snapshot:
    """Everything the I13 read routes serve, as of one build.

    Immutable: a refresh builds a new one and swaps it in, so a request that
    already holds a snapshot keeps reading a consistent one.
    """

    version: int
    reference_date: date
    built_at: datetime
    build_seconds: float
    fingerprint: str

    material_scope_index: Mapping[Key, str | None]
    movement_metrics: tuple[MovementMetrics, ...]
    watch: Mapping[Key, WatchMetric]
    procurement_chain: tuple[PartialLedgerEntry, ...]
    chain_diagnostics: ProcurementChainDiagnostics
    reservation_ledger: tuple[ReservationLedgerEntry, ...]
    legacy_ledger: tuple[LegacyLedgerEntry, ...]
    consumption_attribution: tuple[ConsumptionAttribution, ...]
    reclassification: tuple[ReclassificationCandidate, ...]
    monthly_consumption: Mapping[Key, tuple[MonthlyConsumption, ...]]
    stock_by_key: Mapping[Key, Decimal]
    reference_plans: tuple[ConsumptionPlan, ...]

    # The summary's aging-band counts. Computed at build time because they
    # depend only on SAP data; the plan-dependent counts are not stored here.
    oar_position_count: int
    band_counts: Mapping[str, int]

    notes: tuple[str, ...] = field(default=())

    # --- derived indexes, built on first use and then reused ---------------

    def scope_of(self, material: str, plant: str) -> MaterialScope:
        return classify_material_scope(self.material_scope_index.get((material, plant)))

    @cached_property
    def watch_sorted(self) -> tuple[WatchMetric, ...]:
        return tuple(self.watch[key] for key in sorted(self.watch))

    @cached_property
    def legacy_ledger_by_id(self) -> dict[str, LegacyLedgerEntry]:
        return {entry.ledger_id: entry for entry in self.legacy_ledger}

    @cached_property
    def legacy_ledger_oar(self) -> tuple[LegacyLedgerEntry, ...]:
        return tuple(e for e in self.legacy_ledger if self.scope_of(e.material, e.plant) is MaterialScope.OAR)

    @cached_property
    def reservation_ledger_oar(self) -> tuple[ReservationLedgerEntry, ...]:
        return tuple(e for e in self.reservation_ledger if e.material_scope is MaterialScope.OAR)

    @cached_property
    def grni_entries(self) -> tuple[GrniEntry, ...]:
        """Every reservation-ledger entry that meets the GRNI rule, oldest GR
        first. Same predicate as ``watch._gr_not_issued``, stored per entry."""
        threshold = get_i13_config().watch.gr_not_issued_threshold_days
        found: list[GrniEntry] = []
        for entry in self.reservation_ledger:
            if entry.last_gr_date is None:
                continue
            outstanding = (entry.received_quantity or Decimal("0")) - entry.issued_quantity
            if outstanding <= 0:
                continue
            age = (self.reference_date - entry.last_gr_date).days
            if age < threshold:
                continue
            found.append(GrniEntry(ledger=entry, outstanding_quantity=outstanding, days_since_gr=age))
        found.sort(key=lambda g: (-g.days_since_gr, g.ledger.material, g.ledger.plant, g.ledger.ledger_id))
        return tuple(found)

    @cached_property
    def history_months(self) -> tuple[str, ...]:
        """Every month the movement history covers, in order."""
        months = {m.month for series in self.monthly_consumption.values() for m in series}
        return tuple(sorted(months))


# --- build -------------------------------------------------------------------


def _monthly_consumption(movements: list[Row]) -> dict[Key, tuple[MonthlyConsumption, ...]]:
    issue_reversals = reversal_types_for(ISSUE_TYPES)
    receipt_reversals = reversal_types_for(RECEIPT_TYPES)
    issued: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    issue_events: dict[tuple[str, str, str], int] = defaultdict(int)
    received: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: Decimal("0"))

    for row in movements:
        moved_on = row.get("BudatMkpf")
        if moved_on is None:
            continue
        bucket = (row["Matnr"], row["Werks"], f"{moved_on.year:04d}-{moved_on.month:02d}")
        bwart = row.get("Bwart")
        qty = row.get("Menge") or Decimal("0")
        if bwart in ISSUE_TYPES:
            issued[bucket] += qty
            issue_events[bucket] += 1
        elif bwart in issue_reversals:
            issued[bucket] -= qty
            issue_events[bucket] -= 1
        elif bwart in RECEIPT_TYPES:
            received[bucket] += qty
        elif bwart in receipt_reversals:
            received[bucket] -= qty

    series: dict[Key, list[MonthlyConsumption]] = defaultdict(list)
    for bucket in sorted(set(issued) | set(issue_events) | set(received)):
        material, plant, month = bucket
        series[(material, plant)].append(
            MonthlyConsumption(
                month=month,
                issued_quantity=issued.get(bucket, Decimal("0")),
                # Floored at zero per month, as net_event_count floors overall.
                issue_count=max(issue_events.get(bucket, 0), 0),
                received_quantity=received.get(bucket, Decimal("0")),
            )
        )
    return {key: tuple(values) for key, values in series.items()}


def reference_date_for(settings: Settings | None = None) -> date:
    """The date every snapshot metric is measured as of.

    ``I13_SNAPSHOT_REFERENCE_DATE`` pins it (ISO date); empty means today,
    which is what every live route has always used. Because today is part of
    the fingerprint, a snapshot built yesterday is rebuilt on the first check
    after midnight rather than silently ageing by a day.
    """
    settings = settings or get_settings()
    pinned = (settings.i13_snapshot_reference_date or "").strip()
    return date.fromisoformat(pinned) if pinned else date.today()


def compute_fingerprint(db: Session, *, settings: Settings | None = None, config: I13Config | None = None) -> str:
    """Cheap: one grouped query over ``ingestion_run``, plus the config and date."""
    settings = settings or get_settings()
    config = config or get_i13_config()
    rows = db.execute(
        select(IngestionRun.target_table, func.max(IngestionRun.id), func.max(IngestionRun.finished_at))
        .where(IngestionRun.status == "succeeded", IngestionRun.target_table.in_(FINGERPRINT_TABLES))
        .group_by(IngestionRun.target_table)
        .order_by(IngestionRun.target_table)
    ).all()
    plans_file = Path(settings.i13_data_dir) / "platform" / "consumption_plans.csv"
    plans_stamp = plans_file.stat().st_mtime_ns if plans_file.exists() else 0
    parts = [f"{table}:{run_id}:{finished}" for table, run_id, finished in rows]
    parts += [f"date:{reference_date_for(settings).isoformat()}", f"config:{config!r}", f"plans:{plans_stamp}"]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def build_i13_snapshot(
    db: Session,
    *,
    version: int = 1,
    settings: Settings | None = None,
    config: I13Config | None = None,
) -> I13Snapshot:
    """Run every I13 builder once, sharing repositories, and freeze the result.

    Always does the work; :func:`get_i13_snapshot` is the cached entry point.
    """
    settings = settings or get_settings()
    config = config or get_i13_config()
    data_dir = Path(settings.i13_data_dir)
    as_of = reference_date_for(settings)
    fingerprint = compute_fingerprint(db, settings=settings, config=config)
    started = time.monotonic()

    # ONE instance of each repository for the whole build. Their reads are
    # memoized per instance (``_request_cache``), so the eight builders below
    # read each raw table once between them instead of once each.
    movement_repo = PostgresMovementRepository(db)
    procurement_repo = PostgresProcurementRepository(db)
    reservation_repo = PostgresReservationRepository(db)

    def timed(label: str, fn):
        step_started = time.monotonic()
        result = fn()
        logger.info("I13 snapshot: %s in %.1fs", label, time.monotonic() - step_started)
        return result

    scope_index = timed("material scope index", lambda: fetch_material_scope_index(db))
    movement_metrics = timed(
        "movement metrics",
        lambda: compute_all_movement_metrics(
            movement_repo,
            thresholds=config.aging,
            window_months=config.watch.consumption_window_months,
            as_of=as_of,
        ),
    )
    procurement_chain = timed("procurement chain", lambda: build_procurement_chain(procurement_repo))
    chain_diagnostics = timed("chain diagnostics", lambda: compute_chain_diagnostics(procurement_repo))
    reservation_ledger = timed(
        "reservation ledger",
        lambda: build_reservation_ledger(
            reservation_repo, procurement_repo, material_scope_index=scope_index, include_out_of_scope=True
        ),
    )
    legacy_ledger = timed(
        "legacy ledger",
        lambda: build_legacy_ledger(procurement_repo, reservation_repo, scope_index, include_out_of_scope=True),
    )
    # With `db`, so captured plans count towards acquired-vs-plan (gap G1).
    watch_metrics = timed(
        "WATCH metrics",
        lambda: compute_watch_metrics(
            movement_repo, procurement_repo, reservation_repo, scope_index, config, data_dir, as_of=as_of, db=db
        ),
    )
    reference_plans = tuple(load_reference_plans(data_dir))
    plans = list(reference_plans) + load_captured_plans(db)
    attribution = timed(
        "consumption attribution",
        lambda: ConsumptionAttributionService(
            cost_centre_enabled=config.attribution.cost_centre_enabled
        ).attribute_entries(reservation_ledger, reservation_repo.get_reservations(), plans),
    )
    reclassification = timed(
        "reclassification", lambda: build_reclassification_candidates(movement_repo, scope_index, config, as_of=as_of)
    )
    monthly = timed("monthly consumption", lambda: _monthly_consumption(movement_repo.get_movement_history()))
    stock_by_key = dict(movement_repo.get_current_stock())

    # The summary's band counts, exactly as summary.build_summary counts them:
    # OAR keys only, and an OAR key with no movement history is NON_MOVING.
    oar_keys = {key for key, dismm in scope_index.items() if classify_material_scope(dismm) is MaterialScope.OAR}
    band_counts = {AgingBand.FAST.value: 0, AgingBand.SLOW.value: 0, AgingBand.NON_MOVING.value: 0}
    seen: set[Key] = set()
    for metric in movement_metrics:
        key = (metric.material, metric.plant)
        if key in oar_keys:
            seen.add(key)
            band_counts[metric.aging_band.value] += 1
    band_counts[AgingBand.NON_MOVING.value] += len(oar_keys - seen)

    elapsed = time.monotonic() - started
    logger.info(
        "I13 snapshot v%d built in %.1fs as of %s: %d WATCH rows, %d ledger entries, %d chain entries",
        version,
        elapsed,
        as_of,
        len(watch_metrics),
        len(reservation_ledger),
        len(procurement_chain),
    )
    return I13Snapshot(
        version=version,
        reference_date=as_of,
        built_at=datetime.now(timezone.utc),
        build_seconds=round(elapsed, 1),
        fingerprint=fingerprint,
        material_scope_index=dict(scope_index),
        movement_metrics=tuple(movement_metrics),
        watch={(m.material, m.plant): m for m in watch_metrics},
        procurement_chain=tuple(procurement_chain),
        chain_diagnostics=chain_diagnostics,
        reservation_ledger=tuple(reservation_ledger),
        legacy_ledger=tuple(legacy_ledger),
        consumption_attribution=tuple(attribution),
        reclassification=tuple(reclassification),
        monthly_consumption=monthly,
        stock_by_key=stock_by_key,
        reference_plans=reference_plans,
        oar_position_count=len(oar_keys),
        band_counts=band_counts,
    )


# --- the cache ---------------------------------------------------------------


@dataclass
class _State:
    snapshot: I13Snapshot | None = None
    status: str = "idle"  # idle | building | ready | failed
    building_since: datetime | None = None
    last_error: str | None = None
    last_check: float = 0.0
    failed_at: float = 0.0
    thread: threading.Thread | None = None


_state = _State()
_RETRY_AFTER_FAILURE_SECONDS = 60.0
# Guards `_state`. Held only briefly -- never across a build.
_lock = threading.Lock()
# Serialises builds, so two triggers never spend 40 seconds producing one answer.
_build_lock = threading.Lock()


def _next_version() -> int:
    return (_state.snapshot.version + 1) if _state.snapshot else 1


def _build_and_swap(reason: str) -> I13Snapshot | None:
    with _build_lock:
        with _lock:
            _state.status = "building"
            _state.building_since = datetime.now(timezone.utc)
            version = _next_version()
        logger.info("I13 snapshot build started (%s)", reason)
        db = get_sessionmaker()()
        try:
            snapshot = build_i13_snapshot(db, version=version)
        except Exception as exc:  # noqa: BLE001 -- recorded and surfaced, never swallowed silently
            logger.exception("I13 snapshot build failed (%s)", reason)
            with _lock:
                _state.status = "failed" if _state.snapshot is None else "ready"
                _state.last_error = f"{type(exc).__name__}: {exc}"
                _state.building_since = None
                _state.failed_at = time.monotonic()
            return None
        finally:
            db.close()
        with _lock:
            _state.snapshot = snapshot
            _state.status = "ready"
            _state.last_error = None
            _state.building_since = None
            _state.last_check = time.monotonic()
        return snapshot


def start_background_build(reason: str) -> bool:
    """Build (or rebuild) on a daemon thread. Returns False if one is running."""
    with _lock:
        if _state.thread is not None and _state.thread.is_alive():
            return False
        if _state.snapshot is None:
            _state.status = "building"
            _state.building_since = datetime.now(timezone.utc)
        thread = threading.Thread(target=_build_and_swap, args=(reason,), name="i13-snapshot", daemon=True)
        _state.thread = thread
    thread.start()
    return True


def get_i13_snapshot() -> I13Snapshot:
    """The current snapshot.

    * Available (even mid-rebuild) -> returned at once.
    * Never started -> built now, synchronously (I8's lazy behaviour).
    * First build running in the background -> :class:`SnapshotBuilding`.
    * Last build failed and nothing to serve -> :class:`SnapshotFailed`.
    """
    with _lock:
        snapshot, status = _state.snapshot, _state.status
        building_since, last_error = _state.building_since, _state.last_error
        background = _state.thread is not None and _state.thread.is_alive()
        failed_ago = time.monotonic() - _state.failed_at
    if snapshot is not None:
        return snapshot
    if background:
        raise SnapshotBuilding(building_since)
    if status == "failed":
        # Retried, but not on every request: a build that fails fast (database
        # down) would otherwise be re-attempted by every caller in a tight loop.
        if failed_ago >= _RETRY_AFTER_FAILURE_SECONDS and start_background_build("retry after failure"):
            raise SnapshotBuilding(datetime.now(timezone.utc))
        raise SnapshotFailed(last_error or "unknown error")
    built = _build_and_swap("first request")
    if built is None:
        with _lock:
            raise SnapshotFailed(_state.last_error or "unknown error")
    return built


def peek_i13_snapshot() -> I13Snapshot | None:
    """The current snapshot if there is one; never builds, never raises."""
    with _lock:
        return _state.snapshot


def snapshot_status() -> dict[str, Any]:
    with _lock:
        snapshot = _state.snapshot
        return {
            "status": _state.status,
            "enabled": get_settings().i13_snapshot_enabled,
            "version": snapshot.version if snapshot else None,
            "reference_date": snapshot.reference_date if snapshot else None,
            "built_at": snapshot.built_at if snapshot else None,
            "build_seconds": snapshot.build_seconds if snapshot else None,
            "fingerprint": snapshot.fingerprint if snapshot else None,
            "building_since": _state.building_since,
            "rebuilding": _state.snapshot is not None and _state.status == "building",
            "last_error": _state.last_error,
        }


def check_fingerprint(db: Session, *, min_interval_seconds: float | None = None) -> bool:
    """Start a background rebuild if the data under the snapshot has changed.

    Rate-limited to one check per ``i13_snapshot_check_interval_seconds``.
    Returns True when a rebuild was started.
    """
    interval = (
        get_settings().i13_snapshot_check_interval_seconds if min_interval_seconds is None else min_interval_seconds
    )
    with _lock:
        snapshot = _state.snapshot
        if snapshot is None or time.monotonic() - _state.last_check < interval:
            return False
        _state.last_check = time.monotonic()
    current = compute_fingerprint(db)
    if current == snapshot.fingerprint:
        return False
    logger.info("I13 snapshot fingerprint changed (%s -> %s); rebuilding", snapshot.fingerprint, current)
    return start_background_build("data changed")


def refresh_key(db: Session, material: str, plant: str) -> bool:
    """Recompute one material-plant's WATCH row and swap it into the snapshot.

    Called after the assistant captures a plan: acquired-vs-plan for that
    material now has a plan to measure against. Scoped, so it costs what the
    assistant's own assessment costs (well under a second), not a rebuild.
    Returns False when there is no snapshot to update.
    """
    snapshot = peek_i13_snapshot()
    if snapshot is None:
        return False
    settings = get_settings()
    config = get_i13_config()
    metrics = compute_watch_metrics(
        PostgresMovementRepository(db),
        PostgresProcurementRepository(db),
        PostgresReservationRepository(db),
        fetch_material_scope_index(db, material=material, plant=plant),
        config,
        Path(settings.i13_data_dir),
        material=material,
        plant=plant,
        as_of=snapshot.reference_date,
        db=db,
    )
    fresh = next((m for m in metrics if m.material == material and m.plant == plant), None)
    with _lock:
        current = _state.snapshot
        if current is None:
            return False
        watch = dict(current.watch)
        if fresh is None:
            watch.pop((material, plant), None)
        else:
            watch[(material, plant)] = fresh
        _state.snapshot = replace(current, version=current.version + 1, watch=watch)
    logger.info("I13 snapshot: refreshed WATCH row %s/%s", material, plant)
    return True


def reset_i13_snapshot() -> None:
    """Drop the cached snapshot. For tests."""
    with _lock:
        _state.snapshot = None
        _state.status = "idle"
        _state.building_since = None
        _state.last_error = None
        _state.last_check = 0.0
    with _queue_lock:
        _queue_cache.clear()


# --- request-time results over the snapshot + live plans -------------------


def current_plans(db: Session, snapshot: I13Snapshot) -> list[ConsumptionPlan]:
    """Reference plans (frozen with the snapshot) + captured plans (live)."""
    return list(snapshot.reference_plans) + load_captured_plans(db)


_queue_cache: dict[tuple[int, int], tuple[ExceptionQueueItem, ...]] = {}
_queue_lock = threading.Lock()


def exception_queue(snapshot: I13Snapshot, plans: list[ConsumptionPlan]) -> tuple[ExceptionQueueItem, ...]:
    """The legacy (W6.3) exception queue over the snapshot and these plans.

    Cached per (snapshot version, plans) -- a captured plan changes the key,
    so the summary's no-plan/plan-breach counts move on the next request.
    """
    key = (snapshot.version, hash(tuple((p.plan_id, p.status, p.planned_quantity) for p in plans)))
    with _queue_lock:
        cached = _queue_cache.get(key)
    if cached is not None:
        return cached
    config = get_i13_config()
    items = tuple(
        exception_queue_from(
            list(snapshot.reservation_ledger),
            plans,
            snapshot.watch_sorted,
            config,
            as_of=snapshot.reference_date,
        )
    )
    with _queue_lock:
        _queue_cache.clear()
        _queue_cache[key] = items
    return items

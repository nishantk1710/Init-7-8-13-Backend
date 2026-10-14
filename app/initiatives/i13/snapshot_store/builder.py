"""Build the I13 snapshot into Azure SQL, one batch of materials at a time.

The same computation as ``snapshot.build_i13_snapshot`` -- the same builders,
called the same way -- with three differences, each about memory:

1. The SAP tables are copied once into indexed work tables
   (``work_tables.py``), and every builder reads them through batch-scoped
   repositories (``batch_repos.py``) that return one slice of materials.
2. Each batch's results are written to ``i13_snapshot_record`` and dropped
   before the next batch is read. Peak memory is one batch, not the tenant.
3. Monthly usage keeps the last ``I13_USAGE_HISTORY_MONTHS`` months, and the
   per-movement indexes only FR-6 validation used (issue events, receipt
   dates) are not stored at all -- validation reads SQL itself.

Everything I13 computes is keyed by material-plant, so a batch's slice is
computed as it would have been inside the whole. Two things are not, and are
handled once for the whole build instead of per batch:

* **Plan breaches** are raised per plan, so a plan is evaluated in the batch
  holding its material, and plans whose material is in no batch get a final
  pass of their own. Only the generated reference plans go into the stored
  queue: captured plans change at any moment and are applied when the queue
  is read (``reader.exception_queue``).
* **Session links** are synced with the whole tenant's SGTXT at the end --
  ``sync_links`` deletes every link not in the rows it is given, so a batch's
  rows alone would delete the others'.

A version becomes the served one only after its last batch has landed; the
previous version serves until then, and is deleted after.
"""

from __future__ import annotations

import gc
import json
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.consumption_attribution import ConsumptionAttributionService
from app.initiatives.i13.exceptions import exception_queue_from
from app.initiatives.i13.ledger_compat import build_legacy_ledger
from app.initiatives.i13.models import AgingBand, ProcurementChainDiagnostics
from app.initiatives.i13.movement_metrics import compute_all_movement_metrics
from app.initiatives.i13.plans import ConsumptionPlan, load_reference_plans
from app.initiatives.i13.procurement_chain import (
    build_procurement_chain,
    compute_chain_diagnostics,
)
from app.initiatives.i13.reclassification import build_reclassification_candidates
from app.initiatives.i13.reservation_ledger import build_reservation_ledger
from app.initiatives.i13.session_link import sync_links
from app.initiatives.i13.snapshot_store import kinds, work_tables
from app.initiatives.i13.snapshot_store.batch_repos import (
    Batch,
    BatchMovementRepository,
    BatchMovements,
    BatchProcurementRepository,
    BatchReservationRepository,
    scope_index,
)
from app.initiatives.i13.snapshot_store.work_tables import WorkTables
from app.initiatives.i13.watch import compute_watch_metrics
from app.models.i13_snapshot_store import PAYLOAD_MAX, I13SnapshotRecord, I13SnapshotRun
from app.shared.material_scope import MaterialScope, classify_material_scope

logger = get_logger(__name__)

STATUS_BUILDING = "building"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_SUPERSEDED = "superseded"

#: Rows per INSERT round trip.
_WRITE_CHUNK = 5000
#: Rows per DELETE when an old version is cleared -- small enough that the
#: transaction log of a one-vCore database never has to hold a whole version.
_DELETE_CHUNK = 50000
#: Old run rows kept for history (their records are deleted regardless).
_KEEP_RUN_ROWS = 10


@dataclass
class BuildStats:
    batches: int = 0
    records: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    band_counts: dict[str, int] = field(
        default_factory=lambda: {b.value: 0 for b in AgingBand}
    )
    oar_positions: int = 0
    months: set[str] = field(default_factory=set)
    work_rows: dict[str, int] = field(default_factory=dict)


# --- writing -----------------------------------------------------------------

_INSERT = (
    "INSERT INTO i13_snapshot_record "
    "(version, kind, seq, material, plant, rec_key, oar, f1, f2, f3, n1, d1, text1, payload) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class RecordWriter:
    """Bulk inserts of ``Record``s under one version, numbering them per kind.

    ``write`` takes any iterable and inserts every ``_WRITE_CHUNK`` records as
    it goes, so a generator of a quarter of a million ledger items never sits
    in memory whole -- neither as items nor as insert rows.
    """

    def __init__(self, db: Session, version: int) -> None:
        self.db = db
        self.version = version
        self.seq: dict[str, int] = defaultdict(int)
        self.counts: dict[str, int] = defaultdict(int)

    def write(self, records: Iterable[kinds.Record]) -> None:
        rows = []
        for r in records:
            if len(r.payload) > PAYLOAD_MAX:
                raise ValueError(
                    f"I13 store: a {r.kind} item for {r.material}/{r.plant} is {len(r.payload)} bytes, "
                    f"over the {PAYLOAD_MAX}-byte payload column"
                )
            self.seq[r.kind] += 1
            self.counts[r.kind] += 1
            rows.append(
                (
                    self.version, r.kind, self.seq[r.kind], r.material[:40], r.plant[:10],
                    (r.rec_key or None) and r.rec_key[:100], bool(r.oar),
                    _clip40(r.f1), _clip40(r.f2), _clip40(r.f3), r.n1, r.d1,
                    (r.text1 or None) and r.text1[:400], r.payload,
                )
            )
            if len(rows) >= _WRITE_CHUNK:
                self._insert(rows)
                rows = []
        if rows:
            self._insert(rows)

    def _insert(self, rows: list[tuple]) -> None:
        connection = self.db.connection()
        if connection.dialect.name == "mssql":
            cursor = connection.connection.cursor()
            try:
                import pyodbc

                cursor.fast_executemany = True
                cursor.setinputsizes(
                    [
                        (pyodbc.SQL_INTEGER, 0, 0),
                        (pyodbc.SQL_VARCHAR, 16, 0),
                        (pyodbc.SQL_BIGINT, 0, 0),
                        (pyodbc.SQL_WVARCHAR, 40, 0),
                        (pyodbc.SQL_WVARCHAR, 10, 0),
                        (pyodbc.SQL_WVARCHAR, 100, 0),
                        (pyodbc.SQL_BIT, 0, 0),
                        (pyodbc.SQL_WVARCHAR, 40, 0),
                        (pyodbc.SQL_WVARCHAR, 40, 0),
                        (pyodbc.SQL_WVARCHAR, 40, 0),
                        (pyodbc.SQL_DECIMAL, 28, 6),
                        (pyodbc.SQL_TYPE_DATE, 0, 0),
                        (pyodbc.SQL_WVARCHAR, 400, 0),
                        (pyodbc.SQL_VARCHAR, PAYLOAD_MAX, 0),
                    ]
                )
                cursor.executemany(_INSERT, rows)
            finally:
                cursor.close()
        else:  # pragma: no cover - the app runs on SQL Server only
            columns = ("version", "kind", "seq", "material", "plant", "rec_key", "oar", "f1", "f2", "f3",
                       "n1", "d1", "text1", "payload")
            self.db.execute(I13SnapshotRecord.__table__.insert(), [dict(zip(columns, row)) for row in rows])


def _clip40(value: str | None) -> str | None:
    return value[:40] if value else value


# --- batches -----------------------------------------------------------------


def plan_batches(
    db: Session, work: WorkTables, batch_materials: int, batch_rows: int | None = None
) -> list[Batch]:
    """Contiguous material ranges covering every material any work table holds.

    A batch closes at ``batch_materials`` materials or ``batch_rows`` rows of
    goods movements plus reservations, whichever comes first, so its memory is
    bounded by what it reads, not by how many materials it names: one busy
    reagent can carry more movements than a thousand slow-moving spares. A
    material over ``batch_rows`` on its own gets a batch of its own.

    The materials and their weights come back in SQL Server's own order, and a
    batch is a run of consecutive ones, so the range predicates select exactly
    one batch each.
    """
    union = " UNION ".join(
        f"SELECT material FROM {t}" for t in (work.marc, work.mov, work.stock, work.pr, work.po, work.resb)
    )
    rows = db.execute(
        text(
            f"SELECT u.material, ISNULL(m.n, 0) + ISNULL(r.n, 0) AS weight FROM ({union}) u "
            f"LEFT JOIN (SELECT material, COUNT(*) AS n FROM {work.mov} GROUP BY material) m "
            "ON m.material = u.material "
            f"LEFT JOIN (SELECT material, COUNT(*) AS n FROM {work.resb} GROUP BY material) r "
            "ON r.material = u.material "
            "ORDER BY u.material"
        )
    ).fetchall()
    return cut_batches([(r[0], r[1]) for r in rows], batch_materials, batch_rows)


def cut_batches(
    weighted: list[tuple[str, int]], batch_materials: int, batch_rows: int | None = None
) -> list[Batch]:
    """Consecutive (material, rows) pairs, already in SQL order, cut into
    batches of at most ``batch_materials`` materials and ``batch_rows`` rows. A
    material over ``batch_rows`` on its own is a batch by itself."""
    max_materials = max(batch_materials, 1)
    max_rows = max(batch_rows, 1) if batch_rows else None
    batches: list[Batch] = []
    lo = hi = None
    count = weight = 0
    for material, rows_for_material in weighted:
        if lo is not None and (
            count >= max_materials or (max_rows is not None and weight + rows_for_material > max_rows)
        ):
            batches.append(Batch(lo, hi))
            lo, count, weight = None, 0, 0
        if lo is None:
            lo = material
        hi = material
        count += 1
        weight += rows_for_material
    if lo is not None:
        batches.append(Batch(lo, hi))
    return batches


def _monthly(movements, keep_months: int, as_of: date):
    """``snapshot._monthly_consumption``, limited to the last ``keep_months``."""
    from app.initiatives.i13.snapshot import _monthly_consumption

    first = _month_start(as_of, keep_months - 1)
    series = _monthly_consumption(movements)
    return {key: tuple(m for m in values if m.month >= first) for key, values in series.items()}


def _month_start(as_of: date, months_back: int) -> str:
    year, month = as_of.year, as_of.month - months_back
    while month <= 0:
        month += 12
        year -= 1
    return f"{year:04d}-{month:02d}"


# --- one batch -----------------------------------------------------------------


def _build_batch(
    db: Session,
    work: WorkTables,
    batch: Batch,
    *,
    config: I13Config,
    settings: Settings,
    data_dir: Path,
    as_of: date,
    reference_plans: list[ConsumptionPlan],
    all_plans: list[ConsumptionPlan],
    writer: RecordWriter,
    stats: BuildStats,
    diagnostics: list[ProcurementChainDiagnostics],
    sgtxt_rows: list[dict],
    dated_only: bool = False,
) -> None:
    """One batch's records. ``dated_only`` writes only the kinds measured
    against ``as_of`` (``DATED_KINDS``) -- what a re-age recomputes; the
    others are copied from the version being re-aged (see :func:`reage`)."""
    # Each result is written as soon as it is computed and released as soon as
    # nothing later reads it, so a batch's peak is its largest stage, not the
    # sum of all of them. Kinds are numbered separately, so the order they are
    # written in changes no stored row. What every stage reads is fixed by the
    # repositories, which are memoized: the order they run in changes no value.
    movements = BatchMovements(db, work, batch)  # one read of the movement table for all three
    movement_repo = BatchMovementRepository(db, work, batch, movements)
    procurement_repo = BatchProcurementRepository(db, work, batch, movements)
    reservation_repo = BatchReservationRepository(db, work, batch, movements)

    index = scope_index(db, work, batch)

    def is_oar(material: str, plant: str) -> bool:
        return classify_material_scope(index.get((material, plant))) is MaterialScope.OAR

    # Movement metrics, and the summary band counts exactly as the in-memory
    # build counts them.
    movement_metrics = compute_all_movement_metrics(
        movement_repo,
        thresholds=config.aging,
        window_months=config.watch.consumption_window_months,
        as_of=as_of,
    )
    band_by_key = {(m.material, m.plant): m.aging_band.value for m in movement_metrics}
    oar_keys = [key for key, dismm in index.items() if classify_material_scope(dismm) is MaterialScope.OAR]
    for key in sorted(oar_keys):
        band = band_by_key.get(key, AgingBand.NON_MOVING.value)
        stats.band_counts[band] += 1
    stats.oar_positions += len(oar_keys)
    writer.write(kinds.movement(m, is_oar(m.material, m.plant)) for m in movement_metrics)
    writer.write(kinds.oar_key(k[0], k[1], band_by_key.get(k, AgingBand.NON_MOVING.value)) for k in sorted(oar_keys))
    del movement_metrics, band_by_key, oar_keys

    writer.write(
        kinds.reclass(c, is_oar(c.material, c.plant))
        for c in build_reclassification_candidates(movement_repo, index, config, as_of=as_of)
    )

    watch = sorted(
        compute_watch_metrics(
            movement_repo, procurement_repo, reservation_repo, index, config, data_dir, as_of=as_of, db=db
        ),
        key=lambda m: (m.material, m.plant),
    )
    watch_by_key = {(m.material, m.plant): m for m in watch}

    monthly = _monthly(movement_repo.get_movement_history(), settings.i13_usage_history_months, as_of)
    stock_by_key = movement_repo.get_current_stock()

    def usage_records(monthly, stock_by_key, watch_by_key):
        for key in sorted(monthly):
            series = monthly[key]
            if not series:
                continue
            stats.months.update(m.month for m in series)
            w = watch_by_key.get(key)
            yield kinds.usage(
                key[0], key[1], classify_material_scope(index.get(key)).value, series,
                w.stock_on_hand if w else stock_by_key.get(key),
                w.aging_band.value if w else None,
            )

    writer.write(usage_records(monthly, stock_by_key, watch_by_key))
    # Nothing reads the movement history after this: release the largest list.
    del monthly, stock_by_key, watch_by_key
    del movement_repo
    movements.drop_history()

    if not dated_only:
        writer.write(kinds.chain(e, is_oar(e.material, e.plant)) for e in build_procurement_chain(procurement_repo))
        diagnostics.append(compute_chain_diagnostics(procurement_repo))
        writer.write(
            kinds.lledger(e, is_oar(e.material, e.plant))
            for e in build_legacy_ledger(procurement_repo, reservation_repo, index, include_out_of_scope=True)
        )

    # The reservation ledger is undated, but GRNI and the queue below are
    # measured from it, so a re-age computes it too -- it just does not store it.
    ledger = build_reservation_ledger(
        reservation_repo, procurement_repo, material_scope_index=index, include_out_of_scope=True
    )
    if not dated_only:
        reservation_rows = reservation_repo.get_reservations()
        sgtxt = {(r["Rsnum"], r["Rspos"]): r["Sgtxt"] for r in reservation_rows if r.get("Sgtxt")}
        sgtxt_rows.extend(
            {k: r.get(k) for k in ("Rsnum", "Rspos", "Matnr", "Werks", "Sgtxt", "UatSimulated", "UatStamped")}
            for r in reservation_rows
            if r.get("Sgtxt")
        )
        writer.write(kinds.rledger(e, sgtxt.get((e.reservation_number, e.reservation_item))) for e in ledger)
        del sgtxt

        pr_by_ledger = {e.ledger_id: e.pr_number for e in ledger}
        writer.write(
            kinds.attrib(a, is_oar(a.material, a.plant), pr_by_ledger.get(a.ledger_id))
            for a in ConsumptionAttributionService(
                cost_centre_enabled=config.attribution.cost_centre_enabled
            ).iter_attribute_entries(ledger, reservation_rows, all_plans)
        )
        # The SAP rows are all read now: release them with their repositories.
        del pr_by_ledger, reservation_rows
    del procurement_repo, reservation_repo, movements

    # GRNI per ledger entry -- snapshot.I13Snapshot.grni_entries' rule.
    threshold = config.watch.gr_not_issued_threshold_days

    def grni_records(ledger):
        for entry in ledger:
            if entry.last_gr_date is None:
                continue
            outstanding = (entry.received_quantity or Decimal(0)) - entry.issued_quantity
            age = (as_of - entry.last_gr_date).days
            if outstanding > 0 and age >= threshold:
                yield kinds.grni(entry, outstanding, age)

    writer.write(grni_records(ledger))

    # The stored legacy queue: reference plans only, those of this batch.
    batch_plans = [p for p in reference_plans if batch.holds(p.material)]
    writer.write(
        kinds.exception(item, is_oar(item.material, item.plant))
        for item in exception_queue_from(ledger, batch_plans, watch, config, as_of=as_of)
    )
    del ledger
    writer.write(kinds.watch(m, m.material_scope is MaterialScope.OAR) for m in watch)


# --- the whole build -------------------------------------------------------


def next_version(db: Session) -> int:
    current = db.execute(select(func.max(I13SnapshotRun.version))).scalar()
    return (current or 0) + 1


def build(
    db: Session,
    *,
    reason: str,
    settings: Settings | None = None,
    config: I13Config | None = None,
    progress: Callable[[str], None] | None = None,
) -> int:
    """Build a new version and make it the served one. Returns the version.

    Raises on failure, after marking the run ``failed`` and removing what it
    wrote; the version served before keeps serving.
    """
    from app.initiatives.i13.plans import load_captured_plans
    from app.initiatives.i13.snapshot import compute_fingerprint, reference_date_for

    settings = settings or get_settings()
    config = config or get_i13_config()
    log = progress or logger.info
    data_dir = Path(settings.i13_data_dir)
    as_of = reference_date_for(settings)
    started = time.monotonic()

    version = next_version(db)
    db.add(
        I13SnapshotRun(
            version=version,
            status=STATUS_BUILDING,
            reason=reason[:80],
            fingerprint=compute_fingerprint(db, settings=settings, config=config),
            schema_signature=kinds.signature(),
            reference_date=as_of,
            started_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    log(f"I13 store: build v{version} started ({reason})")

    work = WorkTables(version)
    stats = BuildStats()
    try:
        stats.work_rows = work_tables.create(db.connection(), work, log=log)
        db.commit()

        batches = plan_batches(db, work, settings.i13_build_batch_materials, settings.i13_build_batch_rows)
        stats.batches = len(batches)
        log(
            f"I13 store: v{version} {len(batches)} batches (at most {settings.i13_build_batch_materials} materials "
            f"or {settings.i13_build_batch_rows} rows each)"
        )

        reference_plans = load_reference_plans(data_dir)
        all_plans = list(reference_plans) + load_captured_plans(db)
        writer = RecordWriter(db, version)
        diagnostics: list[ProcurementChainDiagnostics] = []
        sgtxt_rows: list[dict] = []

        for n, batch in enumerate(batches, start=1):
            batch_started = time.monotonic()
            _build_batch(
                db, work, batch,
                config=config, settings=settings, data_dir=data_dir, as_of=as_of,
                reference_plans=reference_plans, all_plans=all_plans,
                writer=writer, stats=stats, diagnostics=diagnostics, sgtxt_rows=sgtxt_rows,
            )
            db.commit()
            gc.collect()
            log(
                f"I13 store: v{version} batch {n}/{len(batches)} ({batch.lo}..{batch.hi}) "
                f"in {time.monotonic() - batch_started:.1f}s"
            )

        # Reference plans whose material no batch holds: breaches only.
        orphans = [p for p in reference_plans if not any(b.holds(p.material) for b in batches)]
        if orphans:
            writer.write([kinds.exception(i, True) for i in exception_queue_from([], orphans, [], config, as_of=as_of)])

        linked = sync_links(db, sgtxt_rows)
        log(f"I13 store: v{version} session links {linked}")

        meta = {
            "oar_positions": stats.oar_positions,
            "band_counts": stats.band_counts,
            "chain_diagnostics": _merge_diagnostics(diagnostics),
            "reference_plan_count": len(reference_plans),
            "history_months": sorted(stats.months),
            "records": dict(writer.counts),
            "work_rows": stats.work_rows,
        }
        elapsed = round(time.monotonic() - started, 1)
        db.execute(
            update(I13SnapshotRun)
            .where(I13SnapshotRun.status == STATUS_READY)
            .values(status=STATUS_SUPERSEDED)
        )
        db.execute(
            update(I13SnapshotRun)
            .where(I13SnapshotRun.version == version)
            .values(
                status=STATUS_READY,
                built_at=datetime.now(timezone.utc),
                build_seconds=Decimal(str(elapsed)),
                batches=len(batches),
                meta=json.dumps(meta, default=str),
            )
        )
        db.commit()
        log(f"I13 store: v{version} ready in {elapsed}s -- {dict(writer.counts)}")
    except Exception as exc:
        db.rollback()
        logger.exception("I13 store: build v%s failed", version)
        db.execute(
            update(I13SnapshotRun)
            .where(I13SnapshotRun.version == version)
            .values(status=STATUS_FAILED, error=f"{type(exc).__name__}: {exc}"[:4000])
        )
        db.commit()
        _clear_version(db, version)
        work_tables.drop(db.connection(), work)
        db.commit()
        raise

    cleanup(db, keep=version)
    return version


# --- a new day: re-age, not rebuild --------------------------------------------
#
# A full build copies the SAP tables, stitches every ledger and writes every
# kind -- about 78 minutes on the production extract. When only the date has
# moved, almost none of that has changed: the ledgers, the procurement chain
# and the attribution depend on the SAP rows alone (kinds.UNDATED_KINDS). A
# re-age therefore reads the served version's own work tables (no SAP copy),
# recomputes only the kinds measured against the date, copies the undated ones
# inside SQL Server, and swaps -- the same all-or-nothing switch a build makes.
# Values are exactly a full build's for that date over the same SAP rows:
# the same builders, called the same way (``_build_batch(dated_only=True)``).

#: Rows per INSERT ... SELECT when the undated kinds are copied: one statement
#: per chunk keeps each transaction's log small on a small database.
_COPY_CHUNK = 200_000


def served_run(db: Session) -> I13SnapshotRun | None:
    """The version being served, if this code can read it."""
    run = db.execute(
        select(I13SnapshotRun)
        .where(I13SnapshotRun.status == STATUS_READY)
        .order_by(I13SnapshotRun.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    return run if run is not None and run.schema_signature == kinds.signature() else None


def _copy_kinds(db: Session, source: int, target: int, names: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for kind in names:
        top = db.execute(
            select(func.max(I13SnapshotRecord.seq)).where(
                I13SnapshotRecord.version == source, I13SnapshotRecord.kind == kind
            )
        ).scalar() or 0
        copied = 0
        for lo in range(0, top, _COPY_CHUNK):
            copied += db.execute(
                text(
                    "INSERT INTO i13_snapshot_record "
                    "(version, kind, seq, material, plant, rec_key, oar, f1, f2, f3, n1, d1, text1, payload) "
                    "SELECT :target, kind, seq, material, plant, rec_key, oar, f1, f2, f3, n1, d1, text1, payload "
                    "FROM i13_snapshot_record "
                    "WHERE version = :source AND kind = :kind AND seq > :lo AND seq <= :hi"
                ),
                {"target": target, "source": source, "kind": kind, "lo": lo, "hi": lo + _COPY_CHUNK},
            ).rowcount
            db.commit()
        counts[kind] = copied
    return counts


def _rename_work_tables(db: Session, source: WorkTables, target: WorkTables) -> None:
    """Hand the source version's work tables to the target, inside the caller's
    transaction (``sp_rename`` is transactional): they hold the same SAP rows,
    and the per-material refresh reads them under the served version's name."""
    for old, new in zip(source.all, target.all):
        db.execute(
            text("IF OBJECT_ID(:old, 'U') IS NOT NULL EXEC sp_rename @objname = :old, @newname = :new"),
            {"old": old, "new": new},
        )


def reage(
    db: Session,
    *,
    reason: str,
    settings: Settings | None = None,
    config: I13Config | None = None,
    progress: Callable[[str], None] | None = None,
) -> int:
    """Bring the served version up to today without re-reading SAP. Returns the
    new version, or the served one when it is already current.

    Raises on failure, after marking the run ``failed`` and removing what it
    wrote; the served version and its work tables are untouched until the
    final swap, which is one transaction.
    """
    from app.initiatives.i13.plans import load_captured_plans
    from app.initiatives.i13.snapshot import reference_date_for

    settings = settings or get_settings()
    config = config or get_i13_config()
    log = progress or logger.info
    data_dir = Path(settings.i13_data_dir)
    as_of = reference_date_for(settings)

    source = served_run(db)
    if source is None:
        raise RuntimeError("I13 store: nothing is served to re-age; a full build is needed")
    if source.reference_date >= as_of:
        log(f"I13 store: v{source.version} is already as of {source.reference_date}; nothing to re-age")
        return source.version

    started = time.monotonic()
    started_at = datetime.now(timezone.utc)
    version = next_version(db)
    db.add(
        I13SnapshotRun(
            version=version,
            status=STATUS_BUILDING,
            reason=reason[:80],
            # The same SAP rows as the source: its fingerprint, so a data change
            # since the source was built is still seen as one.
            fingerprint=source.fingerprint,
            schema_signature=kinds.signature(),
            reference_date=as_of,
            started_at=started_at,
        )
    )
    db.commit()
    log(f"I13 store: re-age v{source.version} ({source.reference_date}) -> v{version} ({as_of}) started ({reason})")

    work = WorkTables(source.version)
    stats = BuildStats()
    try:
        batches = plan_batches(db, work, settings.i13_build_batch_materials, settings.i13_build_batch_rows)
        stats.batches = len(batches)
        reference_plans = load_reference_plans(data_dir)
        all_plans = list(reference_plans) + load_captured_plans(db)
        writer = RecordWriter(db, version)

        for n, batch in enumerate(batches, start=1):
            batch_started = time.monotonic()
            _build_batch(
                db, work, batch,
                config=config, settings=settings, data_dir=data_dir, as_of=as_of,
                reference_plans=reference_plans, all_plans=all_plans,
                writer=writer, stats=stats, diagnostics=[], sgtxt_rows=[], dated_only=True,
            )
            db.commit()
            gc.collect()
            log(
                f"I13 store: re-age v{version} batch {n}/{len(batches)} ({batch.lo}..{batch.hi}) "
                f"in {time.monotonic() - batch_started:.1f}s"
            )

        orphans = [p for p in reference_plans if not any(b.holds(p.material) for b in batches)]
        if orphans:
            writer.write([kinds.exception(i, True) for i in exception_queue_from([], orphans, [], config, as_of=as_of)])
        db.commit()

        copied = _copy_kinds(db, source.version, version, kinds.UNDATED_KINDS)
        log(f"I13 store: re-age v{version} copied {copied}")

        meta = json.loads(source.meta) if source.meta else {}
        meta.update(
            {
                "oar_positions": stats.oar_positions,
                "band_counts": stats.band_counts,
                "reference_plan_count": len(reference_plans),
                "history_months": sorted(stats.months),
                "records": {**copied, **dict(writer.counts)},
                "reaged_from": source.version,
            }
        )
        elapsed = round(time.monotonic() - started, 1)
        db.execute(
            update(I13SnapshotRun).where(I13SnapshotRun.status == STATUS_READY).values(status=STATUS_SUPERSEDED)
        )
        db.execute(
            update(I13SnapshotRun)
            .where(I13SnapshotRun.version == version)
            .values(
                status=STATUS_READY,
                built_at=datetime.now(timezone.utc),
                build_seconds=Decimal(str(elapsed)),
                batches=len(batches),
                meta=json.dumps(meta, default=str),
            )
        )
        _rename_work_tables(db, work, WorkTables(version))
        db.commit()
        log(f"I13 store: re-age v{version} ready in {elapsed}s")
    except Exception as exc:
        db.rollback()
        logger.exception("I13 store: re-age v%s failed", version)
        db.execute(
            update(I13SnapshotRun)
            .where(I13SnapshotRun.version == version)
            .values(status=STATUS_FAILED, error=f"{type(exc).__name__}: {exc}"[:4000])
        )
        db.commit()
        _clear_version(db, version)
        raise

    cleanup(db, keep=version)
    _catch_up(db, since=started_at)
    return version


def _catch_up(db: Session, *, since: datetime) -> None:
    """Re-apply what changed while the re-age ran.

    A plan captured or a UAT reservation simulated during the re-age refreshed
    its material in the source version, which has just been retired. Refresh
    those materials again in the new one -- the same per-material refresh, so
    the result is what it would have been had they arrived a minute later.
    """
    from app.assistant.models import ConsumptionPlanRecord
    from app.initiatives.i13.snapshot_store.refresh import refresh_material
    from app.models.i13_session_link import UatReservationSgtxt

    keys: set[tuple[str, str]] = set()
    for model, stamp in ((ConsumptionPlanRecord, ConsumptionPlanRecord.captured_at),
                         (UatReservationSgtxt, UatReservationSgtxt.created_at)):
        try:
            keys.update(
                (m, p) for m, p in db.execute(select(model.material, model.plant).where(stamp >= since)).all()
            )
        except Exception:
            logger.warning("I13 store: could not list %s changed during the re-age", model.__tablename__, exc_info=True)
            db.rollback()
    for material, plant in sorted(keys):
        try:
            refresh_material(db, material, plant, reservations=True)
            db.commit()
        except Exception:
            db.rollback()
            logger.warning("I13 store: catch-up refresh of %s/%s failed", material, plant, exc_info=True)
    if keys:
        logger.info("I13 store: re-applied %d material(s) changed during the re-age", len(keys))


def _merge_diagnostics(parts: list[ProcurementChainDiagnostics]) -> dict:
    merged: dict = {}
    for part in parts:
        for key, value in asdict(part).items():
            if isinstance(value, list):
                merged.setdefault(key, []).extend([list(v) for v in value])
            else:
                merged[key] = merged.get(key, 0) + value
    if not merged:
        merged = asdict(
            ProcurementChainDiagnostics(0, 0, 0, 0, 0, 0, 0, [], [])
        )
    return merged


def _clear_version(db: Session, version: int) -> None:
    while True:
        deleted = db.execute(
            text(f"DELETE TOP ({_DELETE_CHUNK}) FROM i13_snapshot_record WHERE version = :v"), {"v": version}
        ).rowcount
        db.commit()
        if not deleted:
            break


def cleanup(db: Session, *, keep: int) -> None:
    """Delete every other version's records and work tables; trim old runs."""
    stale = db.execute(
        select(I13SnapshotRecord.version).where(I13SnapshotRecord.version != keep).distinct()
    ).scalars().all()
    for version in stale:
        _clear_version(db, version)
    work_tables.drop_versions_except(db.connection(), {keep})
    db.commit()
    old = db.execute(
        select(I13SnapshotRun.version).order_by(I13SnapshotRun.version.desc()).offset(_KEEP_RUN_ROWS)
    ).scalars().all()
    if old:
        db.execute(delete(I13SnapshotRun).where(I13SnapshotRun.version.in_(old)))
        db.commit()


def abandon_unfinished(db: Session) -> int:
    """Mark builds a restart interrupted as failed and clear what they wrote.

    A build runs in the API process; a restart mid-build leaves its run row
    ``building`` and its records half-written. Called at start-up, before a
    new build. Returns how many were abandoned.
    """
    unfinished = db.execute(
        select(I13SnapshotRun.version).where(I13SnapshotRun.status == STATUS_BUILDING)
    ).scalars().all()
    for version in unfinished:
        db.execute(
            update(I13SnapshotRun)
            .where(I13SnapshotRun.version == version)
            .values(status=STATUS_FAILED, error="interrupted: the process stopped before the build finished")
        )
        db.commit()
        _clear_version(db, version)
    ready = db.execute(
        select(I13SnapshotRun.version).where(I13SnapshotRun.status == STATUS_READY)
    ).scalars().all()
    work_tables.drop_versions_except(db.connection(), set(ready))
    db.commit()
    return len(unfinished)

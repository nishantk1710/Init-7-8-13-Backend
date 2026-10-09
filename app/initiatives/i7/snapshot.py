"""Is the raw layer a COMPLETE snapshot of I07's sources right now?

One question, one answer, and the answer defaults to no.

The only caller that matters is the pipeline orchestrator, deciding what to pass
as ``snapshot_complete``. That flag gates the deactivation sweep, which marks
staged material-plants inactive when a snapshot stops carrying them. Getting it
wrong in one direction leaves a few dead rows in scope for a day; getting it
wrong in the OTHER direction deactivates most of the catalogue, because absence
from a delta means "unchanged", not "deleted". The asymmetry is why every
uncertain, missing, mixed or ambiguous case here resolves to False.

WHAT COUNTS AS EVIDENCE

Four things, all required:

1. Every one of :data:`I7_CSV_TABLES` has a ``csv_extract_request`` that is
   COMPLETE, reconciled, and loaded.
2. They all carry the SAME ``sweep_id``. Membership is read from that column and
   never inferred from ``fired_at`` -- the ingestion contract makes no promise
   that proximity means togetherness, and a sweep runs ~15 minutes, in batches,
   possibly across midnight.
3. ``MaterialPlantSet`` was fetched whole and loaded -- the OData enrichment
   carrying MARC's DISMM/PLIFZ/MINBE/MABST, which the CSV MARC does not have.
4. No OData delta has merged into any I07 raw table SINCE that sweep finished.
   A snapshot plus unmerged increments is not a snapshot: rows the delta added
   were never in the sweep, and sweeping would deactivate them.

WHAT IS DELIBERATELY NOT EVIDENCE

``ingestion_run`` cannot answer this. It has no mode column, and an OData full
pull and an OData delta merge write indistinguishable ``source_file`` values
(``ingest/<service>/<set>/<date>`` vs ``odata:<same>``). ``app.ingest.load``
also deletes earlier rows for a table before inserting, so it is not a history.
It is used here only for (3) and (4), where the question is "did something land
and when", which it does answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.csv_extract import STATUS_COMPLETE, CsvExtractRequest
from app.models.ingestion import IngestionRun

logger = get_logger(__name__)

#: The CSV-delivered tables I07 staging reads, verified against
#: ``app.initiatives.i7.adapters.extract``'s own FROM/JOIN clauses rather than
#: assumed. ``raw_mkpf`` is NOT here: MSEG carries ``BUDAT_MKPF`` denormalised,
#: and staging never joins MKPF -- watching it would demand evidence from a
#: table I07 does not read.
I7_CSV_TABLES: tuple[str, ...] = (
    "MARA",
    "MAKT",
    "MARC",
    "MARD",
    "MBEW",
    "MSEG",
    "EKKO",
    "EKPO",
    "EKET",
    "EKBE",
)

#: The OData set carrying MARC's MRP fields. The CSV MARC extract has no DISMM,
#: PLIFZ, MINBE or MABST at all, so without this the snapshot is missing the
#: fields the OAR rule is evaluated on.
ENRICHMENT_SET = "MaterialPlantSet"

#: The raw tables a delta can merge into that I07 reads. Same list as the CSV
#: tables, plus the enrichment target.
I7_RAW_TABLES: tuple[str, ...] = tuple(f"raw_{t.lower()}" for t in I7_CSV_TABLES) + (
    "odata_material_plant",
)


@dataclass(frozen=True)
class SnapshotVerdict:
    """Whether a complete snapshot is in the raw layer, and why not if not."""

    complete: bool
    sweep_id: str | None = None
    completed_at: datetime | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return self.complete

    def describe(self) -> str:
        if self.complete:
            return f"complete snapshot from sweep {self.sweep_id}"
        return "not a complete snapshot: " + "; ".join(self.reasons)


def _loaded_requests_by_sweep(session: Session) -> dict[str, dict[str, CsvExtractRequest]]:
    """Every COMPLETE+loaded request that names a sweep, grouped by sweep.

    Rows with no ``sweep_id`` are skipped rather than grouped under None: a
    single-table pull is not a sweep, and a row predating the column has no
    knowable membership. Both must read as "no evidence", not as a sweep of one.
    """
    rows = session.execute(
        select(CsvExtractRequest).where(
            CsvExtractRequest.sap_table.in_(I7_CSV_TABLES),
            CsvExtractRequest.status == STATUS_COMPLETE,
            CsvExtractRequest.sweep_id.is_not(None),
            CsvExtractRequest.loaded_at.is_not(None),
        )
    ).scalars().all()

    grouped: dict[str, dict[str, CsvExtractRequest]] = {}
    for row in rows:
        # Newest wins if a table somehow appears twice in one sweep. csv_pull
        # refuses a second OPEN request per table, so this is defensive rather
        # than expected -- but silently picking an arbitrary one would be worse.
        existing = grouped.setdefault(row.sweep_id, {}).get(row.sap_table)
        if existing is None or (row.loaded_at or _MIN) > (existing.loaded_at or _MIN):
            grouped[row.sweep_id][row.sap_table] = row
    return grouped


_MIN = datetime.min


def _reconciled(request: CsvExtractRequest) -> bool:
    """Did this delivery prove it carried the whole extract?

    **Deliberately stricter than ``csv_pull._verdict``**, and the difference is
    the point. That function decides whether a file is loadable, and answers
    COMPLETE in two cases this one must reject:

    * **No ``$count``.** Several sets answer HTTP 500 to ``$count`` while
      serving rows fine, so ``_verdict`` lets the file through saying its
      "completeness is not proven". Not proven is exactly what disqualifies it
      here -- a request with no expectation to check cannot be said to have met
      one.
    * **A ``MaxRows`` cap.** ``--max-rows 100`` is a probe. ``_verdict`` calls
      it COMPLETE because 100 of 100 arrived; it is still 100 rows of a 2,040-
      row table, and treating it as a snapshot would deactivate the other 1,940.

    So a row reaching STATUS_COMPLETE is necessary evidence, never sufficient.
    """
    cap = (request.max_rows or "").strip()
    if cap.isdigit() and int(cap) > 0:
        return False
    if request.expected_rows is None:
        return False
    if request.reconcile == "exact":
        return request.received_rows == request.expected_rows
    # Windowed: the extract is a subset by design, so $count is a ceiling.
    return 0 < request.received_rows <= request.expected_rows


def _enrichment_loaded_after(session: Session, after: datetime | None) -> bool:
    """Did ``MaterialPlantSet`` land after the sweep finished?

    Checked through ``ingestion_run`` because the OData path leaves no request
    row. It cannot tell a full pull from a delta merge -- which is why the
    caller must have fetched it with MODE_FULL, as the daily full refresh does
    (``app.ingest.scheduler.run_full_refresh_cycle``). This confirms it LANDED;
    the full-ness is the scheduler's contract, recorded in its own code.
    """
    row = session.execute(
        select(IngestionRun)
        .where(
            IngestionRun.target_table == "odata_material_plant",
            IngestionRun.status == "succeeded",
        )
        .order_by(IngestionRun.id.desc())
        .limit(1)
    ).scalars().first()
    if row is None:
        return False
    if after is None:
        return True
    finished = row.finished_at
    return finished is not None and finished >= after


def _deltas_since(session: Session, after: datetime) -> list[str]:
    """I07 raw tables an OData delta merged into after the sweep finished.

    ``raw_merge`` writes ``source_file`` as ``odata:<prefix>``; the CSV loader
    writes ``csv:<TABLE>:<request_id>``. That prefix is the only thing in
    ``ingestion_run`` that separates the two paths, and it is enough for this
    narrow question -- "did an OData write touch this table after the sweep?"
    """
    rows = session.execute(
        select(IngestionRun.target_table, IngestionRun.source_file, IngestionRun.finished_at)
        .where(
            IngestionRun.target_table.in_(I7_RAW_TABLES),
            IngestionRun.status == "succeeded",
        )
    ).all()

    touched = []
    for table, source_file, finished in rows:
        if table == "odata_material_plant":
            # The enrichment is PART of the full refresh, not a delta into it.
            continue
        if not (source_file or "").startswith("odata:"):
            continue
        if finished is not None and finished > after:
            touched.append(table)
    return sorted(set(touched))


def resolve_snapshot(session: Session) -> SnapshotVerdict:
    """Decide whether the raw layer currently holds a complete I07 snapshot.

    Never raises for missing evidence -- absence of evidence IS the answer, and
    an exception would make callers choose between crashing and guessing.
    """
    sweeps = _loaded_requests_by_sweep(session)
    if not sweeps:
        return SnapshotVerdict(
            False,
            reasons=("no CSV sweep has completed and loaded for any I07 table",),
        )

    # Newest first: the most recent sweep that qualifies is the snapshot.
    def _sweep_finished(requests: dict[str, CsvExtractRequest]) -> datetime:
        return max((r.loaded_at for r in requests.values() if r.loaded_at), default=_MIN)

    ordered = sorted(sweeps.items(), key=lambda kv: _sweep_finished(kv[1]), reverse=True)
    sweep_id, requests = ordered[0]
    finished = _sweep_finished(requests)

    reasons: list[str] = []

    missing = [table for table in I7_CSV_TABLES if table not in requests]
    if missing:
        reasons.append(
            f"sweep {sweep_id} is missing {len(missing)} table(s): {', '.join(missing)}"
        )

    unreconciled = sorted(
        table for table, request in requests.items() if not _reconciled(request)
    )
    if unreconciled:
        reasons.append(
            f"sweep {sweep_id} did not reconcile for: {', '.join(unreconciled)}"
        )

    if not _enrichment_loaded_after(session, finished if finished != _MIN else None):
        reasons.append(
            f"{ENRICHMENT_SET} has not loaded since sweep {sweep_id} finished; "
            "MARC's DISMM/PLIFZ/MINBE/MABST would be stale or absent"
        )

    if finished != _MIN:
        touched = _deltas_since(session, finished)
        if touched:
            reasons.append(
                "an OData delta merged into "
                f"{', '.join(touched)} after sweep {sweep_id}; the raw layer is "
                "a snapshot plus increments, not a snapshot"
            )

    if reasons:
        verdict = SnapshotVerdict(False, sweep_id=sweep_id, reasons=tuple(reasons))
        logger.info("snapshot check: %s", verdict.describe())
        return verdict

    verdict = SnapshotVerdict(
        True, sweep_id=sweep_id, completed_at=finished if finished != _MIN else None
    )
    logger.info("snapshot check: %s", verdict.describe())
    return verdict

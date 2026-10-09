"""The I07 pipeline orchestrator.

    staging -> features -> forecasting -> inventory -> OAR -> recommendations

**This module contains no business logic.** It calls the same six service
functions ``scripts/run_full_pipeline.py`` calls, in the same order, and does
three things that script does not: it takes a lock so two runs cannot overlap,
it records a durable row per execution, and it stops at the first failure.
Every number it reports was computed by a stage and read back, never derived
here. A figure that looks wrong is a finding about that stage.

WHY THE ORDER IS FIXED AND NOT CONFIGURABLE

Each stage reads the previous one's output from the database. Features read the
staging run; forecasting reads the feature store; inventory reads forecasts; OAR
borrows from inventory-eligible neighbours; recommendations read inventory and
OAR. Skipping one does not shorten the pipeline, it makes the next stage read
whatever the *last* run left behind -- which is exactly the class of silent
staleness this work exists to remove. ``scripts/run_full_pipeline.py`` keeps its
``--skip`` for interactive debugging; the automated path has no such flag.

WHY THE LOCK IS THE SAME ONE THE INGEST SCHEDULER USES

``app.ingest.scheduler._Lock`` is ``sp_getapplock`` with ``@LockOwner='Session'``
-- held for the life of a connection and released by SQL Server if the worker
dies. That last property is what makes stale-run recovery possible at all: a row
saying ``running`` is not evidence a run is alive, because a killed process
never gets to write anything. The lock is the liveness signal; the row is the
audit trail. Reusing it rather than writing a second mechanism also means the
two cannot disagree about what "held" means.

ON RETRIES

A retry is a NEW pipeline run that reuses the idempotent stages' existing work
where they offer it (``force=False`` on inventory and OAR, their own default).
It is not a resumption: there is no checkpoint to resume from, because a stage
either wrote its run row or did not. Marking the previous attempt ABANDONED and
starting a fresh row keeps every attempt visible, which a mutated retry count on
one row would not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.models.i7_pipeline import (
    STATUS_ABANDONED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    PipelineRun,
)

logger = get_logger(__name__)

#: Unique to this job. The ingest scheduler's two locks are different mutexes;
#: a pipeline run and a delta fetch may legitimately overlap, because the
#: pipeline reads the normalise views and the fetch writes raw tables -- and
#: staging reading a table mid-load is a staleness question, not a corruption
#: one: the next refresh triggers the next run.
LOCK_NAME = "spares_ai_i7_pipeline"

#: In order. The name is what lands in ``stage_statuses`` and ``failed_stage``,
#: so it is also what an operator reads.
STAGES: tuple[str, ...] = (
    "staging",
    "features",
    "forecasting",
    "inventory",
    "oar",
    "recommendations",
)


class PipelineBusy(RuntimeError):
    """Another pipeline run holds the lock. Not an error condition.

    Raised rather than queued: the run that holds the lock is already doing the
    work this one would do, over the same source data. Waiting behind it would
    only run the identical pipeline again the moment it finished.
    """


@dataclass
class PipelineResult:
    """What one execution did. Every count is read back from a stage."""

    run_id: int | None = None
    status: str = STATUS_SUCCEEDED
    stages: dict[str, str] = field(default_factory=dict)
    failed_stage: str | None = None
    error: str | None = None

    staging_run_id: int | None = None
    feature_run_id: int | None = None
    forecast_run_id: int | None = None
    inventory_run_id: int | None = None
    oar_run_id: int | None = None
    recommendations_written: int = 0

    deactivated: int = 0

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCEEDED

    def stage_statuses_text(self) -> str:
        """``"staging=succeeded;features=failed"`` -- the stored form."""
        return ";".join(f"{name}={self.stages[name]}" for name in STAGES if name in self.stages)


def _abandon_stale(session: Session, *, lock_held: bool) -> int:
    """Close out ``running`` rows that no live process owns. Returns how many.

    Only ever called while THIS process holds the pipeline lock. That is the
    whole argument: the lock is exclusive, so any other row still claiming to
    run cannot have a live owner -- its process died without writing, and its
    lock was released by the server. Without this, one killed run would leave a
    row that reads as in-flight forever.

    ``lock_held`` is required and asserted rather than inferred, so a future
    caller cannot reach this from outside the locked section and mass-abandon
    a healthy concurrent run.
    """
    if not lock_held:
        raise RuntimeError("_abandon_stale called without the pipeline lock")

    stale = session.execute(
        update(PipelineRun)
        .where(PipelineRun.status == STATUS_RUNNING)
        .values(
            status=STATUS_ABANDONED,
            finished_at=datetime.now(timezone.utc),
            error=(
                "abandoned: still marked running when a later run took the "
                "lock, so the process that owned it is gone"
            ),
        )
    )
    count = stale.rowcount or 0
    if count:
        logger.warning(
            "%d stale pipeline run(s) marked abandoned -- they held no lock", count
        )
    return count


def _stage_callables() -> dict[str, Callable[[], object]]:
    """The six stage entry points, imported lazily.

    Lazy because forecasting pulls in statsmodels and LightGBM: importing this
    module must stay cheap enough for the API process, which imports it to
    expose run status and may never execute a pipeline.
    """
    from app.initiatives.i7.adapters.extract import stage_extract
    from app.initiatives.i7.features.builder import build_features
    from app.initiatives.i7.forecasting.service import run_forecasting
    from app.initiatives.i7.inventory.service import run_inventory_calculations
    from app.initiatives.i7.oar.service import run_oar_similarity
    from app.initiatives.i7.recommendations.service import generate_recommendations

    return {
        "staging": stage_extract,
        "features": build_features,
        "forecasting": run_forecasting,
        "inventory": run_inventory_calculations,
        "oar": run_oar_similarity,
        "recommendations": generate_recommendations,
    }


def _record_stage(
    session_factory, run_id: int, result: PipelineResult, stage: str
) -> None:
    """Persist progress after each stage, so a crash leaves a readable row."""
    with session_factory() as session:
        session.execute(
            update(PipelineRun)
            .where(PipelineRun.id == run_id)
            .values(
                stage_statuses=result.stage_statuses_text(),
                staging_run_id=result.staging_run_id,
                feature_run_id=result.feature_run_id,
                forecast_run_id=result.forecast_run_id,
                inventory_run_id=result.inventory_run_id,
                oar_run_id=result.oar_run_id,
                heartbeat_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    logger.info("pipeline run %d: %s %s", run_id, stage, result.stages[stage])


def run_pipeline(
    *,
    trigger_reason: str = "manual",
    snapshot_complete: bool = False,
    source_fingerprint: str | None = None,
    lock=None,
) -> PipelineResult:
    """Run the I07 pipeline end to end, once.

    ``snapshot_complete`` is passed straight to staging and gates the
    deactivation sweep. It **defaults False**, and the caller is the only thing
    that can know the answer: only a run following a verified-successful CSV
    full refresh may pass True, because only that path replaces ``raw_<table>``
    whole. A delta refresh, a partial refresh, or any uncertainty must leave it
    False -- absence from a delta means "unchanged", and sweeping on one would
    deactivate almost the entire catalogue.

    Raises :class:`PipelineBusy` when another run holds the lock. Never raises
    for a stage failure: that is recorded on the run row and returned, because a
    failed pipeline is an outcome to read, not an exception to handle.
    """
    if lock is None:
        from app.ingest.scheduler import _Lock

        lock = _Lock(LOCK_NAME)

    if not lock.acquire():
        raise PipelineBusy(
            "another I07 pipeline run holds the lock; it is already processing "
            "this source data"
        )

    session_factory = get_sessionmaker()
    result = PipelineResult()

    try:
        # Inside the lock: a stale row can only be judged dead by a process that
        # has proven no live run exists, and holding the lock is that proof.
        with session_factory() as session:
            _abandon_stale(session, lock_held=True)
            session.commit()

            run = PipelineRun(
                status=STATUS_RUNNING,
                trigger_reason=trigger_reason[:128],
                source_fingerprint=source_fingerprint,
                snapshot_complete=snapshot_complete,
                heartbeat_at=datetime.now(timezone.utc),
            )
            session.add(run)
            session.commit()
            run_id = run.id

        result.run_id = run_id
        logger.info(
            "pipeline run %d starting (%s, snapshot_complete=%s)",
            run_id,
            trigger_reason,
            snapshot_complete,
        )

        stages = _stage_callables()
        result.status = STATUS_RUNNING

        for stage in STAGES:
            try:
                outcome = _run_one(stage, stages[stage], snapshot_complete, source_fingerprint)
            except Exception as exc:  # noqa: BLE001 -- recorded, not propagated
                detail = f"{type(exc).__name__}: {exc}"
                logger.exception("pipeline run %d: %s raised", run_id, stage)
                result.stages[stage] = STATUS_FAILED
                result.failed_stage = stage
                result.error = detail
                result.status = STATUS_FAILED
                break

            result.stages[stage] = outcome.status
            _absorb(result, stage, outcome)
            _record_stage(session_factory, run_id, result, stage)

            if outcome.status != STATUS_SUCCEEDED:
                # Stop here. Every later stage reads this one's output, so
                # continuing would compute from the PREVIOUS run's rows and
                # publish them as though they were this run's.
                result.failed_stage = stage
                result.error = getattr(outcome, "error", None) or f"{stage} reported {outcome.status}"
                result.status = STATUS_FAILED
                logger.error(
                    "pipeline run %d: stopping at %s -- %s", run_id, stage, result.error
                )
                break
        else:
            result.status = STATUS_SUCCEEDED

        _finalise(session_factory, run_id, result)
        return result

    finally:
        lock.release()


@dataclass
class _Outcome:
    """What one stage returned, flattened to what the orchestrator needs."""

    status: str
    error: str | None = None
    run_id: int | None = None
    written: int = 0
    deactivated: int = 0


def _run_one(
    stage: str,
    call: Callable[..., object],
    snapshot_complete: bool,
    source_fingerprint: str | None,
) -> _Outcome:
    """Call one stage and normalise its result.

    The stage signatures differ -- staging takes the snapshot flag, inventory
    and OAR take ``force``, recommendations takes neither -- so the only place
    that knows the differences is here, and it is a dispatch rather than a
    wrapper around each service.
    """
    if stage == "staging":
        outcome = call(
            snapshot_complete=snapshot_complete, source_fingerprint=source_fingerprint
        )
        return _Outcome(
            status=outcome.status,
            error=outcome.error,
            run_id=outcome.run_id,
            deactivated=outcome.deactivated,
        )

    outcome = call()
    return _Outcome(
        status=outcome.status,
        error=getattr(outcome, "error", None),
        run_id=getattr(outcome, "run_id", None),
        written=getattr(outcome, "recommendations_written", 0),
    )


def _absorb(result: PipelineResult, stage: str, outcome: _Outcome) -> None:
    """Copy a stage's identifiers onto the pipeline result."""
    if stage == "staging":
        result.staging_run_id = outcome.run_id
        result.deactivated = outcome.deactivated
    elif stage == "features":
        result.feature_run_id = outcome.run_id
    elif stage == "forecasting":
        result.forecast_run_id = outcome.run_id
    elif stage == "inventory":
        result.inventory_run_id = outcome.run_id
    elif stage == "oar":
        result.oar_run_id = outcome.run_id
    elif stage == "recommendations":
        result.recommendations_written = outcome.written


def _finalise(session_factory, run_id: int, result: PipelineResult) -> None:
    """Write the terminal state. Best effort -- the result is returned either
    way, because losing the audit row must not also lose the outcome."""
    try:
        with session_factory() as session:
            session.execute(
                update(PipelineRun)
                .where(PipelineRun.id == run_id)
                .values(
                    status=result.status,
                    stage_statuses=result.stage_statuses_text(),
                    failed_stage=result.failed_stage,
                    error=(result.error or None) and result.error[:4000],
                    staging_run_id=result.staging_run_id,
                    feature_run_id=result.feature_run_id,
                    forecast_run_id=result.forecast_run_id,
                    inventory_run_id=result.inventory_run_id,
                    oar_run_id=result.oar_run_id,
                    recommendations_written=result.recommendations_written,
                    finished_at=datetime.now(timezone.utc),
                    heartbeat_at=datetime.now(timezone.utc),
                )
            )
            session.commit()
    except Exception:
        logger.exception("pipeline run %d: could not record the final state", run_id)

    logger.info(
        "pipeline run %d %s (%s)", run_id, result.status, result.stage_statuses_text()
    )


def run_pipeline_for_current_source() -> PipelineResult:
    """Run the pipeline against whatever is in the raw layer now, resolving
    both the fingerprint and the snapshot verdict from the database.

    This is the entry point a trigger uses, and the only place
    ``snapshot_complete`` is DERIVED rather than passed in. Keeping that
    derivation here means no caller can assert a complete snapshot by asserting
    a keyword argument: the evidence is read from
    ``app.initiatives.i7.snapshot.resolve_snapshot``, which requires all ten CSV
    tables reconciled and loaded under one verified sweep, the OData enrichment
    after it, and no delta merged since.

    ``run_pipeline`` keeps its explicit parameters for tests and for an operator
    who has established the answer another way.
    """
    from app.initiatives.i7.snapshot import resolve_snapshot
    from app.initiatives.i7.watch import source_fingerprint

    session_factory = get_sessionmaker()
    with session_factory() as session:
        fingerprint = source_fingerprint(session)
        verdict = resolve_snapshot(session)

    if not verdict.complete:
        # Not an error, and not a reason to skip the run: the pipeline still
        # stages, forecasts and recommends. Only the deactivation sweep is
        # withheld, because absence of evidence is not evidence of deletion.
        logger.info(
            "pipeline: running without the deactivation sweep -- %s",
            verdict.describe(),
        )

    return run_pipeline(
        trigger_reason="source data changed",
        snapshot_complete=verdict.complete,
        source_fingerprint=fingerprint,
    )


def latest_run(session: Session) -> PipelineRun | None:
    """The most recent pipeline run, whatever its state."""
    return session.execute(
        select(PipelineRun).order_by(PipelineRun.id.desc()).limit(1)
    ).scalars().first()


def last_successful_fingerprint(session: Session) -> str | None:
    """The source fingerprint of the newest SUCCEEDED run.

    Phase C compares against this, not against ``latest_run``: a failed run must
    not advance the watermark, or a refresh that broke the pipeline would never
    be retried.
    """
    return session.execute(
        select(PipelineRun.source_fingerprint)
        .where(PipelineRun.status == STATUS_SUCCEEDED)
        .order_by(PipelineRun.id.desc())
        .limit(1)
    ).scalar()

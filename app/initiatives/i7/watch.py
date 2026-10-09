"""Has I07's source data changed since the last successful pipeline run?

Two functions and a decision. :func:`source_fingerprint` reduces the raw layer's
load history to one string; :func:`should_run` compares it to the fingerprint
the last SUCCEEDED pipeline run recorded and answers whether to run again.

MODELLED ON I08/I13, NOT COPIED FROM THEM

``app.initiatives.i8.service.source_state`` does the same reduction over its own
tables, and the shape is deliberately the same: one grouped query over
``ingestion_run``, the run id in the fingerprint as well as the timestamp so a
reload finishing within a clock tick still registers. Three things differ, and
each is a consequence of what I07 does rather than a preference:

* **No reference date.** I13's fingerprint includes the date because its
  snapshot ages -- yesterday's aging buckets are wrong today. I07's pipeline
  output does not age that way: a forecast computed from unchanged inputs is
  still that forecast tomorrow. Including the date would rerun the whole
  pipeline nightly for no new data, which is the opposite of the point.
* **The comparison is persisted, not in-memory.** I08 compares against the
  snapshot held in the process. I07 compares against
  ``i7_pipeline_run.source_fingerprint``, so a restart cannot lose the fact
  that a fingerprint was already processed -- and a run interrupted by a deploy
  is retried rather than silently skipped.
* **Only SUCCEEDED runs advance it.** A failed pipeline must not record its
  fingerprint as done, or a refresh that broke the pipeline would never be
  retried.

WHY THIS CANNOT TRIGGER ITSELF

The fingerprint reads ``ingestion_run``, which only the raw-layer loaders write
(``app.ingest.csv_load``, ``app.ingest.load``, ``app.ingest.raw_merge``). The
pipeline writes ``i7_staged_*``, ``i7_*_run`` and ``i7_pipeline_run`` -- none of
which the fingerprint reads. The two sets are disjoint, so a pipeline run cannot
move its own trigger. That is a property of the table split, not a guard that
could be forgotten.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i7.pipeline import last_successful_fingerprint
from app.initiatives.i7.snapshot import I7_RAW_TABLES
from app.models.ingestion import IngestionRun

logger = get_logger(__name__)


@dataclass(frozen=True)
class TriggerDecision:
    """Whether to start a pipeline run, and why."""

    run: bool
    fingerprint: str
    reason: str

    def __bool__(self) -> bool:
        return self.run


def source_fingerprint(session: Session) -> str:
    """What the I07 raw layer looks like right now, as one comparable string.

    Built only from ``succeeded`` loads: a failed or partial load must not move
    the fingerprint, or the pipeline would run against data the loader itself
    says is not there. A table that has never loaded simply contributes nothing,
    which is the honest representation -- it is absent, not empty.
    """
    rows = session.execute(
        select(
            IngestionRun.target_table,
            func.max(IngestionRun.id),
            func.max(IngestionRun.finished_at),
        )
        .where(
            IngestionRun.status == "succeeded",
            IngestionRun.target_table.in_(I7_RAW_TABLES),
        )
        .group_by(IngestionRun.target_table)
        .order_by(IngestionRun.target_table)
    ).all()
    return "|".join(f"{table}:{run_id}:{finished}" for table, run_id, finished in rows)


def should_run(session: Session) -> TriggerDecision:
    """Decide whether the source data has moved since the last good run.

    Never raises on missing history: a database with no pipeline runs and no
    loads is a legitimate state, and the answer there is "nothing to do".
    """
    fingerprint = source_fingerprint(session)

    if not fingerprint:
        return TriggerDecision(
            False, fingerprint, "no successful load of any I07 source table yet"
        )

    previous = last_successful_fingerprint(session)
    if previous is None:
        return TriggerDecision(
            True, fingerprint, "no successful pipeline run has processed this data yet"
        )

    if previous == fingerprint:
        return TriggerDecision(
            False, fingerprint, "source data unchanged since the last successful run"
        )

    return TriggerDecision(True, fingerprint, "source data changed")


def check_and_run() -> str:
    """One tick: look, and run the pipeline only if the source has moved.

    Returns a one-line description of what happened, for the caller to log. Never
    raises -- a tick that cannot reach the database must not kill the loop that
    calls it, and a pipeline failure is already recorded on its own run row.

    Concurrency is the orchestrator's existing lock, not a second mechanism
    here: if a run is already in flight, ``run_pipeline`` raises
    :class:`~app.initiatives.i7.pipeline.PipelineBusy` and this reports it as a
    skip. That also covers the case this function cannot see -- a pipeline
    started by hand or by another instance between the check and the call.
    """
    from app.core.db import get_sessionmaker
    from app.initiatives.i7.pipeline import PipelineBusy, run_pipeline_for_current_source

    try:
        session_factory = get_sessionmaker()
        with session_factory() as session:
            decision = should_run(session)
    except Exception as exc:  # noqa: BLE001 -- a failed check must not kill the loop
        logger.exception("I07 trigger check failed")
        return f"check failed: {type(exc).__name__}: {exc}"

    if not decision.run:
        logger.debug("I07 pipeline not triggered: %s", decision.reason)
        return f"no run: {decision.reason}"

    logger.info("I07 pipeline triggered: %s", decision.reason)
    try:
        result = run_pipeline_for_current_source()
    except PipelineBusy as exc:
        logger.info("I07 pipeline already running; skipping this tick")
        return f"skipped: {exc}"
    except Exception as exc:  # noqa: BLE001
        logger.exception("I07 pipeline raised outside its own error handling")
        return f"raised: {type(exc).__name__}: {exc}"

    return f"run {result.run_id}: {result.status} ({result.stage_statuses_text()})"

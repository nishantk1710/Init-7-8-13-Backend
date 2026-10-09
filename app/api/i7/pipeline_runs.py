"""Read-only I07 pipeline-run status.

Lists and reads ``i7_pipeline_run`` exactly as the orchestrator wrote it. Like
``runs.py``, this module **runs no pipeline stage and starts no pipeline** --
a GET never triggers work. Starting a run is
``app.initiatives.i7.pipeline.run_pipeline``, called by an operator or a
trigger, never by a request.

Mounted at ``/pipeline-runs``, not under ``/runs``: ``runs.py`` owns
``/runs/{run_type}/{run_id}``, and a sibling ``/runs/pipeline`` would be
matched by it with ``run_type="pipeline"`` -- a 422 on an enum that does not
include it. A distinct prefix avoids the collision rather than depending on
registration order to resolve it.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.i7.deps import get_session
from app.models.i7_pipeline import PipelineRun
from app.schemas.i7.errors import not_found
from app.schemas.i7.pipeline_runs import (
    PipelineRunDetail,
    PipelineRunListResponse,
    PipelineRunSummary,
    PipelineStage,
)

router = APIRouter(tags=["i7-pipeline-runs"])


def _stages(row: PipelineRun) -> list[PipelineStage]:
    """``"staging=succeeded;features=failed"`` back into objects.

    Stored as text because ``app/models/base.py`` allows only portable
    constructs -- no JSONB -- and six fixed keys did not justify a child table.
    Parsed here rather than in the model so the storage form stays the model's
    business and the API shape stays the API's.
    """
    raw = (row.stage_statuses or "").strip()
    if not raw:
        return []
    stages = []
    for part in raw.split(";"):
        name, _, status = part.partition("=")
        if name and status:
            stages.append(PipelineStage(name=name, status=status))
    return stages


def _to_summary(row: PipelineRun) -> PipelineRunSummary:
    return PipelineRunSummary(
        run_id=row.id,
        status=row.status,
        trigger_reason=row.trigger_reason,
        failed_stage=row.failed_stage,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


@router.get(
    "/pipeline-runs",
    response_model=PipelineRunListResponse,
    summary="List I07 pipeline runs",
    description="The most recent end-to-end pipeline executions, newest first. "
    "Read-only -- lists existing rows, never triggers a run.",
)
def list_pipeline_runs(
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=100, description="Most recent runs")] = 20,
) -> PipelineRunListResponse:
    rows = session.execute(
        select(PipelineRun).order_by(PipelineRun.id.desc()).limit(limit)
    ).scalars().all()
    return PipelineRunListResponse(items=[_to_summary(row) for row in rows])


@router.get(
    "/pipeline-runs/latest",
    response_model=PipelineRunDetail,
    summary="Get the most recent I07 pipeline run",
    description="The newest run whatever its state -- running, succeeded, "
    "failed or abandoned. Declared before /{run_id} so 'latest' is never "
    "parsed as an id.",
    responses={404: {"description": "No pipeline run has been recorded"}},
)
def latest_pipeline_run(
    session: Annotated[Session, Depends(get_session)],
) -> PipelineRunDetail:
    row = session.execute(
        select(PipelineRun).order_by(PipelineRun.id.desc()).limit(1)
    ).scalars().first()
    if row is None:
        raise not_found(
            "PIPELINE_RUN_NOT_FOUND", "No pipeline run has been recorded."
        )
    return _to_detail(row)


@router.get(
    "/pipeline-runs/{run_id}",
    response_model=PipelineRunDetail,
    summary="Get one I07 pipeline run",
    responses={404: {"description": "Run not found"}},
)
def get_pipeline_run(
    run_id: int, session: Annotated[Session, Depends(get_session)]
) -> PipelineRunDetail:
    row = session.get(PipelineRun, run_id)
    if row is None:
        raise not_found(
            "PIPELINE_RUN_NOT_FOUND", "Pipeline run was not found.", run_id=run_id
        )
    return _to_detail(row)


def _to_detail(row: PipelineRun) -> PipelineRunDetail:
    return PipelineRunDetail(
        run_id=row.id,
        status=row.status,
        trigger_reason=row.trigger_reason,
        stages=_stages(row),
        failed_stage=row.failed_stage,
        error=row.error,
        source_fingerprint=row.source_fingerprint,
        snapshot_complete=row.snapshot_complete,
        staging_run_id=row.staging_run_id,
        feature_run_id=row.feature_run_id,
        forecast_run_id=row.forecast_run_id,
        inventory_run_id=row.inventory_run_id,
        oar_run_id=row.oar_run_id,
        recommendations_written=row.recommendations_written,
        started_at=row.started_at,
        finished_at=row.finished_at,
        heartbeat_at=row.heartbeat_at,
    )

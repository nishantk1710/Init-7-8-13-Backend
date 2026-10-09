"""I07 pipeline-run API schemas.

Separate from ``runs.py`` rather than folded into it, and the reason is in that
module's own schemas: ``RunSummary`` and ``RunDetail`` require ``policy_id`` and
``policy_version``, because every one of the four STAGE runs they describe is
executed under a policy. A pipeline run is not -- it orchestrates stages, each
of which resolves its own policy. Adding "pipeline" to ``RunType`` would mean
either inventing a policy for it or making two required fields optional for
every existing consumer, and both are worse than a second, honest shape.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PipelineStage(BaseModel):
    """One stage's outcome within a run."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: str


class PipelineRunSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: int
    status: str
    trigger_reason: str
    failed_stage: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class PipelineRunDetail(BaseModel):
    """Everything recorded about one execution.

    The stage run ids are included so a number published by this pipeline is
    traceable to the stage that computed it, without a second query per stage.
    """

    model_config = ConfigDict(frozen=True)

    run_id: int
    status: str
    trigger_reason: str

    stages: list[PipelineStage]
    failed_stage: str | None = None
    error: str | None = None

    source_fingerprint: str | None = None
    snapshot_complete: bool
    """Whether this run was entitled to run the deactivation sweep. False means
    the sweep did not run -- not that it found nothing."""

    staging_run_id: int | None = None
    feature_run_id: int | None = None
    forecast_run_id: int | None = None
    inventory_run_id: int | None = None
    oar_run_id: int | None = None
    recommendations_written: int

    started_at: datetime
    finished_at: datetime | None = None
    heartbeat_at: datetime | None = None


class PipelineRunListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[PipelineRunSummary]

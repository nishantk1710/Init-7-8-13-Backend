"""The I07 pipeline run: one durable record per end-to-end execution.

The four existing run tables (``i7_feature_run``, ``i7_forecast_run``,
``i7_inventory_run``, ``i7_oar_run``) each record one STAGE. None of them
records the *execution* that ran them all, so "did the 02:00 refresh finish?"
had no row to read -- only four tables to correlate by timestamp and hope.

This is that row. It is deliberately a sibling of the stage tables rather than
their parent: the stage services are unchanged and still own their own runs, and
this points AT them rather than replacing them.

**Durability is the point.** A run is recorded before any stage starts and
updated as each finishes, so a process killed mid-pipeline leaves a row saying
exactly which stage was in flight. The alternative -- writing the row at the end
-- would make a crashed run indistinguishable from one that never started.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Lifecycle. A run is RUNNING from creation until exactly one terminal state.
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_ABANDONED = "abandoned"
"""Was RUNNING, holds no lock, and the process that owned it is gone. Recorded
by the next run rather than by the dead one -- a process killed by the platform
does not get to write anything, which is precisely why a row that only it could
close would stay RUNNING forever and block every later run."""


class PipelineRun(Base):
    """One end-to-end I07 execution: staging through recommendations."""

    __tablename__ = "i7_pipeline_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    status: Mapped[str] = mapped_column(String(32), index=True)

    trigger_reason: Mapped[str] = mapped_column(String(128))
    """Why this ran: ``"manual"``, ``"source data changed"``, ``"retry of 41"``.
    Free text on purpose -- it is read by a person asking what happened, and an
    enum would have to grow every time a new caller appears."""

    source_fingerprint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """The raw-layer state this run read, as ``app.shared.source_state`` builds
    it. The column Phase C's watcher compares against to decide whether anything
    has changed since -- and the reason that comparison survives a restart."""

    snapshot_complete: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("0")
    )
    """Whether this run was entitled to run the deactivation sweep. Passed
    through to ``stage_extract``; recorded here too so the decision is visible
    on the run that made it, not only on the staging run it produced."""

    # --- What it produced -------------------------------------------------
    #
    # The stage run ids, so every number this pipeline published is reachable
    # from one row. Nullable because a run that failed at staging never had a
    # forecast run to point at, and zero would be a lie about run 0.
    staging_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    feature_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forecast_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inventory_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    oar_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    recommendations_written: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0")
    )

    # --- What happened ----------------------------------------------------
    stage_statuses: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Per-stage outcome as compact text, e.g.
    ``"staging=succeeded;features=succeeded;forecasting=failed"``.

    Text rather than JSON because ``app/models/base.py`` allows only portable
    constructs -- no ``JSONB`` -- and a child table for six fixed keys would be
    a join for something a person reads as one line."""

    failed_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """Which stage ended the run. The first question an operator asks, so it is
    a column rather than something to parse back out of ``stage_statuses``."""

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Updated as each stage completes. With the application lock this is
    belt-and-braces: the lock is the authority on whether a run is alive, and
    this says how far it had got and when -- which the lock cannot, because a
    released lock leaves nothing behind."""

    def __repr__(self) -> str:
        return f"<PipelineRun {self.id} status={self.status} stage={self.failed_stage}>"

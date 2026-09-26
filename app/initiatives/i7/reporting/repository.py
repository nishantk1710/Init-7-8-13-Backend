"""I07 Quarterly Deep-Dive Report -- persistence repository.

Thin read/write layer over ``i7_quarterly_report`` (see
``app/models/i7_reporting.py``). No aggregation logic lives here -- that is
``service.generate_quarterly_report``'s job; this module only stores and
retrieves its output.

**Idempotency**: ``save_report`` upserts by ``quarter`` alone (see the model
module's docstring for why) -- generating the same quarter twice overwrites
that quarter's one row rather than creating a second. There is no
database-level ``ON CONFLICT`` here (a Postgres-only construct the
portability rule excludes) -- ``save_report`` does a plain get-then-update-
or-insert under the caller's own session/transaction.

**List-vs-detail size discipline** (mirrors ``RecommendationSummary`` vs
``RecommendationDetail``): ``list_reports`` returns lightweight summaries
(quarter/status/generated_at/report_version + the run-id provenance columns)
without touching ``report_json`` -- a listing of many quarters should not pull
a full multi-section report payload per row. ``get_report`` returns the full
ORM row (including ``report_json``); deserializing that JSON back into a
``QuarterlyReport`` is left to the caller (the API layer), which already
owns the Pydantic schema import and is the natural place to decide whether a
raw dict or a validated model is wanted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.i7_reporting import QuarterlyReportRecord
from app.schemas.i7.reports import QuarterlyReport

logger = get_logger(__name__)


@dataclass(frozen=True)
class QuarterlyReportSummary:
    """Lightweight listing row -- no ``report_json``."""

    id: int
    quarter: str
    period_start: date
    period_end: date
    status: str
    report_version: str
    generated_at: datetime
    feature_run_id: int | None
    forecast_run_id: int | None
    inventory_run_id: int | None
    oar_run_id: int | None


def _to_summary(row: QuarterlyReportRecord) -> QuarterlyReportSummary:
    return QuarterlyReportSummary(
        id=row.id,
        quarter=row.quarter,
        period_start=row.period_start,
        period_end=row.period_end,
        status=row.status,
        report_version=row.report_version,
        generated_at=row.generated_at,
        feature_run_id=row.feature_run_id,
        forecast_run_id=row.forecast_run_id,
        inventory_run_id=row.inventory_run_id,
        oar_run_id=row.oar_run_id,
    )


def save_report(
    session: Session,
    quarter: str,
    report: QuarterlyReport,
    status: str = "COMPLETED",
    *,
    error: str | None = None,
    feature_run_id: int | None = None,
    forecast_run_id: int | None = None,
    inventory_run_id: int | None = None,
    oar_run_id: int | None = None,
) -> QuarterlyReportRecord:
    """Persist ``report`` for ``quarter``, overwriting any existing row for
    that quarter (upsert-by-quarter -- see the module and model docstrings).

    Called with ``status="COMPLETED"`` and a fully-assembled ``report`` on
    the success path; a caller that wants to record a FAILED attempt passes
    ``status="FAILED"`` and ``error`` set, in which case ``report`` should
    still be a valid (if a placeholder/last-known) ``QuarterlyReport`` --
    this function does not accept a missing report, keeping "there is no
    report for this quarter" (no row) distinct from "the report failed"
    (a row with ``status=FAILED``).
    """
    row = session.execute(
        select(QuarterlyReportRecord).where(QuarterlyReportRecord.quarter == quarter)
    ).scalar_one_or_none()

    report_json = report.model_dump_json()

    if row is None:
        row = QuarterlyReportRecord(quarter=quarter)
        session.add(row)

    row.period_start = report.metadata.period_start
    row.period_end = report.metadata.period_end
    row.generated_at = report.metadata.generated_at
    row.status = status
    row.report_version = report.metadata.report_version
    row.report_json = report_json
    row.error = error
    row.feature_run_id = feature_run_id
    row.forecast_run_id = forecast_run_id
    row.inventory_run_id = inventory_run_id
    row.oar_run_id = oar_run_id

    session.flush()

    logger.info(
        "i7.reporting.quarterly.save",
        extra={"quarter": quarter, "status": status, "report_id": row.id},
    )
    return row


def get_report(session: Session, quarter: str) -> QuarterlyReportRecord | None:
    """The row for ``quarter``, or ``None`` if it has never been generated.

    Returns the ORM row (including ``report_json``) rather than a
    deserialized ``QuarterlyReport`` -- deserialization is left to the
    caller, which already owns the Pydantic import and can decide whether it
    needs the validated model or just the raw JSON text (e.g. to pass
    straight through as a response body without a round-trip)."""
    return session.execute(
        select(QuarterlyReportRecord).where(QuarterlyReportRecord.quarter == quarter)
    ).scalar_one_or_none()


def list_reports(session: Session, limit: int = 20) -> list[QuarterlyReportSummary]:
    """Most recently generated reports first, summaries only (no
    ``report_json`` -- see the module docstring)."""
    rows = (
        session.execute(
            select(QuarterlyReportRecord)
            .order_by(QuarterlyReportRecord.generated_at.desc(), QuarterlyReportRecord.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [_to_summary(row) for row in rows]

"""I07 Quarterly Deep-Dive Report -- API endpoints.

Thin translation layer only. All aggregation lives in
``app.initiatives.i7.reporting.service.generate_quarterly_report``; all
persistence in ``app.initiatives.i7.reporting.repository``; this router never
computes a figure itself.

**Generation is synchronous.** There is no job queue or background-task
infrastructure anywhere in this codebase (confirmed: no celery, no
apscheduler). ``POST /reports/quarterly/generate`` runs
``generate_quarterly_report`` in the request/response cycle and persists the
result before responding -- timed during this session's own end-to-end smoke
test at roughly 40-50 seconds against the current data volumes (226,930
``i7_recommendation`` rows). That is slow for a typical API call but still
well inside a normal HTTP request timeout (and Azure App Service's default),
so a synchronous call remains the correct scope today rather than a
background-task workaround this codebase has no infrastructure for -- a
caller (frontend or curl) should be built expecting a multi-second-to-
roughly-a-minute response, not an instant one. The
``status`` field on every response (``GenerationStatus``: PENDING/RUNNING/
COMPLETED/FAILED) and the dedicated ``GET .../status`` polling endpoint exist
so the API shape does not need to change if generation later becomes
asynchronous -- a frontend that already polls for COMPLETED needs no update
when/if a real async path is introduced.

**Idempotency** is enforced one layer down: ``repository.save_report`` upserts
by ``quarter`` alone (see that module's docstring), so calling
``POST .../generate`` twice for the same quarter overwrites the one row for
that quarter rather than creating a second.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.i7.deps import get_session
from app.core.logging import get_logger
from app.initiatives.i7.reporting.report_export import build_quarterly_report_workbook
from app.initiatives.i7.reporting.repository import get_report, list_reports, save_report
from app.initiatives.i7.reporting.service import generate_quarterly_report
from app.schemas.i7.errors import bad_request, not_found
from app.schemas.i7.reports import (
    GenerateReportRequest,
    GenerationStatus,
    GenerationStatusResponse,
    QuarterlyReport,
    QuarterlyReportListItem,
    QuarterlyReportListResponse,
)

router = APIRouter(tags=["i7-reports"])

logger = get_logger(__name__)

MAX_LIST_LIMIT = 50


@router.get(
    "/reports/quarterly",
    response_model=QuarterlyReportListResponse,
    summary="List generated I07 Quarterly Deep-Dive Reports",
    description="Summaries only (quarter/status/generated_at/report_id) -- "
    "no report_json. Registered before the /{quarter} routes so 'quarterly' "
    "itself is never read as a path with no quarter.",
)
def list_quarterly_reports(
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = 20,
) -> QuarterlyReportListResponse:
    summaries = list_reports(session, limit=limit)
    return QuarterlyReportListResponse(
        items=[QuarterlyReportListItem.from_summary(s) for s in summaries],
        total=len(summaries),
    )


@router.post(
    "/reports/quarterly/generate",
    response_model=QuarterlyReport,
    summary="Generate (or regenerate) the I07 Quarterly Deep-Dive Report",
    description="Synchronous: runs the aggregation and persists the result "
    "before responding (see module docstring for why). Idempotent per "
    "quarter -- calling this twice for the same quarter overwrites that "
    "quarter's one row rather than creating a duplicate.",
)
def generate_quarterly_report_endpoint(
    request: GenerateReportRequest, session: Annotated[Session, Depends(get_session)]
) -> QuarterlyReport:
    quarter = request.quarter
    logger.info("i7.api.reports.generate.start", extra={"quarter": quarter})
    try:
        report = generate_quarterly_report(session, quarter)
    except ValueError as exc:
        raise bad_request("INVALID_QUARTER", str(exc), quarter=quarter) from exc

    try:
        save_report(session, quarter, report, status="COMPLETED")
        session.commit()
    except Exception as exc:
        session.rollback()
        # Best-effort failure record -- a FAILED row still needs a valid
        # QuarterlyReport payload per save_report's contract, so the
        # successfully-generated (but not yet persisted) report is reused
        # rather than fabricating a placeholder.
        try:
            save_report(session, quarter, report, status="FAILED", error=str(exc))
            session.commit()
        except Exception:
            session.rollback()
        logger.info("i7.api.reports.generate.failed", extra={"quarter": quarter})
        raise

    logger.info("i7.api.reports.generate.done", extra={"quarter": quarter})
    return report


@router.get(
    "/reports/quarterly/{quarter}/status",
    response_model=GenerationStatusResponse,
    summary="Generation status for one quarter",
    description="For a frontend polling loop -- kept as its own endpoint even "
    "though generation is synchronous today, so the API shape is ready for an "
    "async implementation without a breaking change. Registered before the "
    "bare /{quarter} route so 'status' is never read as a quarter value.",
)
def get_quarterly_report_status(
    quarter: str, session: Annotated[Session, Depends(get_session)]
) -> GenerationStatusResponse:
    row = get_report(session, quarter)
    if row is None:
        return GenerationStatusResponse(quarter=quarter, status=GenerationStatus.PENDING)
    return GenerationStatusResponse(
        quarter=quarter,
        status=GenerationStatus(row.status),
        report_id=row.id,
        generated_at=row.generated_at,
        error=row.error,
    )


@router.get(
    "/reports/quarterly/{quarter}/export",
    summary="Raw/detail Excel export for one quarter",
    description="NOT the management report -- that is the JSON GET below, "
    "which the frontend renders. This is a secondary openpyxl-built workbook "
    "of the row-level detail behind Section 12's baseline comparison, for a "
    "reader who wants a spreadsheet. Registered before the bare /{quarter} "
    "route so 'export' is never read as a quarter value.",
    responses={404: {"description": "Report not found or not yet generated"}},
)
def export_quarterly_report(
    quarter: str, session: Annotated[Session, Depends(get_session)]
) -> StreamingResponse:
    row = get_report(session, quarter)
    if row is None or row.report_json is None:
        raise not_found(
            "REPORT_NOT_FOUND",
            "Report was not found or has not been generated yet.",
            quarter=quarter,
        )
    report = QuarterlyReport.from_report_json(row.report_json)
    workbook = build_quarterly_report_workbook(report)
    filename = f"i7-quarterly-report-{quarter.replace(' ', '_')}.xlsx"
    return StreamingResponse(
        workbook,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/reports/quarterly/{quarter}",
    response_model=QuarterlyReport,
    summary="Get one quarter's full I07 Quarterly Deep-Dive Report",
    description="The full report body (all 14 sections) -- the management "
    "report the frontend renders. 404 if this quarter has never been "
    "generated.",
    responses={404: {"description": "Report not found or not yet generated"}},
)
def get_quarterly_report(
    quarter: str, session: Annotated[Session, Depends(get_session)]
) -> QuarterlyReport:
    row = get_report(session, quarter)
    if row is None or row.report_json is None:
        raise not_found(
            "REPORT_NOT_FOUND",
            "Report was not found or has not been generated yet.",
            quarter=quarter,
        )
    return QuarterlyReport.from_report_json(row.report_json)

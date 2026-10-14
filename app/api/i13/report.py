"""The I13 summary report API. See ``app.initiatives.i13.report``.

Behind ``I13_REPORT_ENABLED`` (off by default): while off, every route here
answers 404 -- the same answer as a backend without the feature, which is how
the frontend's optional sections already decide to hide themselves.

Generating reads the served snapshot only (``require_snapshot``): while the
first build is running it answers 503 like every I13 screen, rather than
falling back to a live recompute that would compete with the build for Azure
SQL. Reading a saved report touches only its own table.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.i13.deps import Actor, get_current_actor, require_snapshot
from app.core.config import get_settings
from app.core.db import get_db
from app.initiatives.i13.report import store
from app.initiatives.i13.report.export import build_report_workbook
from app.initiatives.i13.report.facts import build_report_facts
from app.initiatives.i13.report.narrative import write_narrative
from app.initiatives.i13.snapshot import I13Snapshot
from app.initiatives.i13.snapshot_store.reader import SqlSnapshot
from app.models.i13_summary_report import I13SummaryReport
from app.schemas.i13 import SummaryReportListItem, SummaryReportResponse


def require_report_enabled() -> None:
    if not get_settings().i13_report_enabled:
        raise HTTPException(status_code=404, detail="The I13 summary report is not enabled.")


router = APIRouter(prefix="/reports", tags=["i13-report"], dependencies=[Depends(require_report_enabled)])


def _list_item(row: I13SummaryReport) -> SummaryReportListItem:
    return SummaryReportListItem(
        id=row.id,
        generated_at=row.generated_at,
        generated_by=row.generated_by,
        plant=row.plant,
        data_as_of=row.data_as_of,
        headline=row.headline,
        narrative_source=row.narrative_source,
    )


def _response(row: I13SummaryReport) -> SummaryReportResponse:
    saved = store.payload(row)
    return SummaryReportResponse(
        **_list_item(row).model_dump(), facts=saved["facts"], narrative=saved["narrative"]
    )


def _found(db: Session, report_id: str) -> I13SummaryReport:
    row = store.get_report(db, report_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No summary report {report_id!r}.")
    return row


@router.post("/summary", response_model=SummaryReportResponse, status_code=201)
def generate_summary_report(
    plant: str | None = Query(None, description="One plant only (1300 or 1500). Omitted: every plant."),
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
    snapshot: I13Snapshot | SqlSnapshot = Depends(require_snapshot),
) -> SummaryReportResponse:
    """Generate and save one report. Never fails for want of an AI provider:
    the words fall back to a fixed template, and ``narrative.source`` says so."""
    facts = build_report_facts(snapshot, db, plant=plant or None)
    narrative = write_narrative(facts)
    row = store.save(db, facts=facts, narrative=narrative, generated_by=actor.id, plant=plant or None)
    db.commit()
    return _response(row)


@router.get("", response_model=list[SummaryReportListItem])
def list_summary_reports(
    plant: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[SummaryReportListItem]:
    """Newest first."""
    return [_list_item(row) for row in store.list_reports(db, plant=plant, limit=limit)]


@router.get("/{report_id}", response_model=SummaryReportResponse)
def get_summary_report(report_id: str, db: Session = Depends(get_db)) -> SummaryReportResponse:
    return _response(_found(db, report_id))


@router.get("/{report_id}/export.xlsx")
def export_summary_report(report_id: str, db: Session = Depends(get_db)) -> Response:
    row = _found(db, report_id)
    meta = {
        "id": row.id,
        "generated_at": row.generated_at.strftime("%Y-%m-%d %H:%M UTC") if row.generated_at else None,
        "generated_by": row.generated_by,
    }
    workbook = build_report_workbook(meta, store.payload(row))
    stamp = row.data_as_of.isoformat() if row.data_as_of else row.id[:8]
    filename = f"oar-utilization-report-{row.plant or 'all-plants'}-{stamp}.xlsx"
    return Response(
        content=workbook.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

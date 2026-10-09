"""GET /api/i13/validation -- FR-6 reconciliation against ZMM065 and the 30-Day GR Report.

Both reports are read from the serving database (``postgres_reports``) and
compared like with like -- see ``app.initiatives.i13.report_validation`` for
what each report actually measures and why the comparisons are shaped the way
they are:

* ZMM065: the platform's aging band per material-plant, computed as of the
  report's run date, against the report's own class -- per band and per row.
* 30-Day GR Report: every receipt it lists, confirmed against the platform's
  PO lines and goods-receipt postings.

A report that is not loaded yields its result rows as REFERENCE_UNAVAILABLE and
its detail block as null.

ZMM065 is uploaded monthly by VZI (``POST /validation/zmm065/uploads``, see
``app.initiatives.i13.zmm065_upload``). Without ``report_month`` validation reads
the latest upload per plant, falling back to the seeded July workbook for a
plant with none; with ``report_month`` it reads only that month's uploads.

The two ``*_reference_count`` query parameters predate the reports being
loaded, when a person typed the counts in. They are still accepted so an older
client does not error, and ignored: a typed count was compared against figures
that were not the same kind of thing (see the module above).
"""

from collections import defaultdict
from dataclasses import asdict
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.api.i13.deps import Actor, get_current_actor, snapshot_or_live
from app.core.db import get_db
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.movements import ISSUE_TYPES, RECEIPT_TYPES, last_unreversed_date, reversal_types_for
from app.initiatives.i13.procurement_chain import build_procurement_chain
from app.initiatives.i13.report_validation import (
    Gr30DayValidation,
    Zmm065Validation,
    validate_gr_30day,
    validate_zmm065,
)
from app.initiatives.i13.snapshot import I13Snapshot
from app.initiatives.i13.zmm065_upload import (
    Zmm065DuplicateUpload,
    Zmm065UploadError,
    current_uploads,
    list_uploads,
    parse_month,
    parse_zmm065_workbook,
    store_upload,
    zmm065_reference,
)
from app.integrations.sap.postgres_movements import PostgresMovementRepository
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reports import fetch_gr_30day_rows
from app.schemas.i13 import (
    Gr30DayValidationResponse,
    ReconciliationSourceResult,
    ValidationResponse,
    Zmm065SourceResponse,
    Zmm065UploadResponse,
    Zmm065UploadResultResponse,
    Zmm065ValidationResponse,
)

router = APIRouter()

Key = tuple[str, str]

#: The largest workbook accepted. The July reports are ~3 MB each.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


@router.get("/validation", response_model=ValidationResponse)
def get_validation(
    zmm065_reference_count: int | None = Query(
        None, description="Deprecated and ignored: ZMM065 is now read from the database."
    ),
    gr_30_day_reference_count: int | None = Query(
        None, description="Deprecated and ignored: the 30-Day GR Report is now read from the database."
    ),
    report_month: str | None = Query(
        None,
        description="YYYY-MM: reconcile against that month's ZMM065 uploads only. "
        "Omitted: the latest upload per plant, else the seeded report.",
    ),
    db: Session = Depends(get_db),
    config: I13Config = Depends(get_i13_config),
    snapshot: I13Snapshot | None = Depends(snapshot_or_live),
) -> ValidationResponse:
    tolerance = config.reconciliation.tolerance_pct
    if snapshot is not None:
        issue_events, history_start = snapshot.issue_events, snapshot.movement_history_start
        po_line_plant = _po_line_plant(snapshot.procurement_chain)
        receipt_dates = snapshot.receipt_dates_by_po_line
    else:
        issue_events, history_start, po_line_plant, receipt_dates = _live_inputs(db)

    def last_issue_as_of(key: Key, day: date) -> date | None:
        return last_unreversed_date(list(issue_events.get(key, ())), ISSUE_TYPES, as_of=day)

    try:
        month = parse_month(report_month) if report_month else None
    except Zmm065UploadError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from None
    zmm065_rows, zmm065_sources = zmm065_reference(db, month)
    zmm065 = (
        validate_zmm065(
            zmm065_rows,
            last_issue_as_of=last_issue_as_of,
            history_start=history_start,
            thresholds=config.aging,
            tolerance_pct=tolerance,
        )
        if zmm065_rows is not None
        else None
    )
    gr_rows = fetch_gr_30day_rows(db)
    gr = (
        validate_gr_30day(gr_rows, po_line_plant=po_line_plant, receipt_dates=receipt_dates, tolerance_pct=tolerance)
        if gr_rows is not None
        else None
    )

    results = (
        list(zmm065.results)
        if zmm065
        else [_unavailable(f"ZMM065 · {band}") for band in ("Fast moving", "Slow moving", "Non-moving")]
    )
    results.append(gr.result if gr else _unavailable("30-Day GR Report · receipts confirmed"))
    return ValidationResponse(
        tolerance_pct=Decimal(str(tolerance)),
        results=[ReconciliationSourceResult.model_validate(r) for r in results],
        zmm065=_zmm065_response(zmm065) if zmm065 else None,
        gr_30_day=_gr_response(gr) if gr else None,
        zmm065_report_month=month,
        zmm065_sources=[Zmm065SourceResponse.model_validate(source) for source in zmm065_sources],
    )


@router.get("/validation/zmm065/uploads", response_model=list[Zmm065UploadResponse])
def get_zmm065_uploads(db: Session = Depends(get_db)) -> list[Zmm065UploadResponse]:
    """Every ZMM065 upload, newest month first. ``is_current`` marks the one
    per plant that validation reads by default."""
    uploads = list_uploads(db)
    current = {upload.id for upload in current_uploads(uploads).values()}
    return [
        Zmm065UploadResponse.model_validate(upload).model_copy(update={"is_current": upload.id in current})
        for upload in uploads
    ]


@router.post(
    "/validation/zmm065/uploads",
    response_model=Zmm065UploadResultResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_zmm065(
    file: UploadFile = File(..., description="One site's ZMM065 aging report, .xlsx"),
    report_month: str = Form(..., description="YYYY-MM, the month the report is for"),
    replace: bool = Form(False, description="Supersede an earlier upload for the same plant and month"),
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> Zmm065UploadResultResponse:
    """Store one month's ZMM065 report for one plant. The plant is read from
    the rows. 409 when that plant and month is already uploaded and ``replace``
    is not set; 422 when the file is not a usable ZMM065 workbook."""
    file_name = file.filename or "zmm065.xlsx"
    if not file_name.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{file_name} is not an Excel workbook; upload the ZMM065 export as .xlsx.",
        )
    if file.size is not None and file.size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{file_name} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )
    try:
        month = parse_month(report_month)
        parsed = parse_zmm065_workbook(file.file)
        upload, replaced_earlier = store_upload(
            db, parsed, report_month=month, file_name=file_name, uploaded_by=actor.id, replace=replace
        )
    except Zmm065DuplicateUpload as duplicate:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(duplicate)) from None
    except Zmm065UploadError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from None
    return Zmm065UploadResultResponse(
        upload=Zmm065UploadResponse.model_validate(upload).model_copy(update={"is_current": True}),
        skipped_out_of_scope=parsed.skipped_out_of_scope,
        skipped_blank=parsed.skipped_blank,
        replaced_earlier=replaced_earlier,
    )


def _po_line_plant(procurement_chain) -> dict[Key, str]:
    return {
        (entry.po_number, entry.po_item): entry.plant
        for entry in procurement_chain
        if entry.po_number and entry.po_item
    }


def _live_inputs(db: Session):
    """The snapshot's validation inputs, read on demand (``?live=true`` or the
    snapshot switched off). Slow on real data, as every live I13 route is."""
    movement_repo = PostgresMovementRepository(db)
    procurement_repo = PostgresProcurementRepository(db)
    wanted = set(ISSUE_TYPES) | reversal_types_for(ISSUE_TYPES)
    issue_events: dict[Key, list] = defaultdict(list)
    history_start: date | None = None
    for row in movement_repo.get_movement_history():
        moved_on = row.get("BudatMkpf")
        if moved_on is not None and (history_start is None or moved_on < history_start):
            history_start = moved_on
        if row.get("Bwart") in wanted:
            issue_events[(row["Matnr"], row["Werks"])].append(row)
    receipt_dates: dict[Key, set[date]] = defaultdict(set)
    for row in procurement_repo.get_goods_receipt_history():
        if row.get("Bwart") in RECEIPT_TYPES and row.get("BudatMkpf") is not None:
            receipt_dates[(row["Ebeln"], row["Ebelp"])].add(row["BudatMkpf"])
    po_line_plant = _po_line_plant(build_procurement_chain(procurement_repo))
    return issue_events, history_start, po_line_plant, receipt_dates


def _unavailable(source_name: str) -> dict:
    return {
        "source_name": source_name,
        "computed_count": 0,
        "reference_count": None,
        "absolute_difference": None,
        "percentage_difference": None,
        "within_tolerance": None,
        "status": "REFERENCE_UNAVAILABLE",
    }


def _zmm065_response(result: Zmm065Validation) -> Zmm065ValidationResponse:
    return Zmm065ValidationResponse(
        report_date=result.report_date,
        rows_in_report=result.rows_in_report,
        compared=result.compared,
        agreed=result.agreed,
        agreement_pct=result.agreement_pct,
        excluded_non_aging=dict(result.excluded_non_aging),
        mismatch_reasons=dict(result.mismatch_reasons),
        mismatches=[
            {
                **asdict(m),
                "report_band": m.report_band.value,
                "platform_band": m.platform_band.value,
                "reason": m.reason.value,
            }
            for m in result.mismatches
        ],
    )


def _gr_response(result: Gr30DayValidation) -> Gr30DayValidationResponse:
    return Gr30DayValidationResponse(
        report_date=result.report_date,
        window_start=result.window_start,
        rows_in_report=result.rows_in_report,
        confirmed=result.confirmed,
        plants=dict(result.plants),
        platform_receipts_in_window=result.platform_receipts_in_window,
        unconfirmed=[{**asdict(c), "status": c.status.value} for c in result.unconfirmed],
    )

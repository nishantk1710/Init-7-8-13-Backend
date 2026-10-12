"""Upload and list the monthly criticality reports.

Errors are plain ``HTTPException(detail=<sentence>)``, not the I07 error
envelope: the frontend's API client shows ``detail`` to the uploader, and a
sentence is what it can show.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_current_actor
from app.core.db import get_db
from app.integrations.criticality import reports
from app.integrations.criticality.report_parser import CriticalityReportError, parse_month, parse_report
from app.shared.plant_scope import IN_SCOPE_PLANTS, plant_name

router = APIRouter(prefix="/criticality", tags=["criticality"])

#: Same ceiling as the other workbook upload.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class PlantResponse(BaseModel):
    code: str
    name: str
    #: The plant's current report month, or ``None`` before anything is loaded.
    current_month: date | None
    materials: int


class UploadResponse(BaseModel):
    id: int
    plant: str
    plant_name: str
    report_month: date
    file_name: str
    #: ``upload`` or ``july_delivery``.
    source: str
    #: ``None`` for the July delivery.
    uploaded_by: str | None
    uploaded_at: datetime
    row_count: int
    #: ``current``, ``previous`` or ``replaced``.
    status: str


class UploadStatsResponse(BaseModel):
    rows_read: int
    tiers: int
    duplicates: int
    blank_tiers: int
    skipped_blank_material: int
    #: The plant the file itself named. Always equals the plant picked -- a
    #: different one is refused -- and returned so the page can say so.
    file_plant: str


class UploadResultResponse(BaseModel):
    upload: UploadResponse
    stats: UploadStatsResponse
    #: Whether this upload's month is now the plant's current report. False for
    #: a back-dated month, which only fills what newer reports leave out.
    is_current: bool
    current_month: date
    replaced_upload_id: int | None
    file_stored: bool


def _entry(entry: reports.UploadEntry) -> UploadResponse:
    upload = entry.upload
    return UploadResponse(
        id=upload.id,
        plant=upload.plant,
        plant_name=plant_name(upload.plant),
        report_month=upload.report_month,
        file_name=upload.file_name,
        source=upload.source,
        uploaded_by=upload.uploaded_by,
        uploaded_at=upload.uploaded_at,
        row_count=upload.row_count,
        status=entry.status,
    )


def _require_plant(code: str) -> str:
    plant = (code or "").strip()
    if plant not in IN_SCOPE_PLANTS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Plant {code!r} is not one of the platform's plants ({', '.join(IN_SCOPE_PLANTS)}).",
        )
    return plant


@router.get("/plants", response_model=list[PlantResponse])
def get_plants(db: Session = Depends(get_db)) -> list[PlantResponse]:
    """The plants a report can be uploaded for, with each one's current month."""
    current = reports.current_by_plant(db)
    materials = reports.materials_by_plant(db)
    return [
        PlantResponse(
            code=code,
            name=plant_name(code),
            current_month=current[code].report_month if code in current else None,
            materials=materials.get(code, 0),
        )
        for code in IN_SCOPE_PLANTS
    ]


@router.get("/uploads", response_model=list[UploadResponse])
def get_uploads(plant: str | None = None, db: Session = Depends(get_db)) -> list[UploadResponse]:
    """Every upload, newest month first, with ``status`` -- ``current`` is the
    one per plant every initiative reads from."""
    return [_entry(entry) for entry in reports.list_uploads(db, plant.strip() if plant else None)]


@router.post("/uploads", response_model=UploadResultResponse, status_code=status.HTTP_201_CREATED)
def upload_report(
    plant: str = Form(..., description="The plant the report was run for"),
    report_month: str = Form(..., description="YYYY-MM, the month the report is for"),
    file: UploadFile = File(..., description="That plant's criticality report, .xlsx"),
    replace: bool = Form(False, description="Replace an earlier upload for the same plant and month"),
    db: Session = Depends(get_db),
    actor: Actor = Depends(get_current_actor),
) -> UploadResultResponse:
    """Store one month's report for one plant. It becomes current at once if
    its month is the newest the plant has. 409 when that plant and month is
    already uploaded and ``replace`` is not set; 422 when the file is not a
    usable report for the plant picked."""
    plant = _require_plant(plant)
    file_name = file.filename or "criticality-report.xlsx"
    if not file_name.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{file_name} is not an Excel workbook; upload the report as .xlsx.",
        )
    handle = file.file
    handle.seek(0, 2)
    size = handle.tell()
    handle.seek(0)
    if size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{file_name} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )

    try:
        month = parse_month(report_month)
        # Refuse a duplicate before reading the workbook or archiving it.
        if not replace:
            same_month = [u for u in reports.effective_uploads(db, plant) if u.report_month == month]
            if same_month:
                raise reports.DuplicateMonthError(same_month[0])
        parsed = parse_report(handle, plant=plant)
        digest = reports.sha256_of(handle)
        storage_key = reports.keep_original(handle, plant=plant, report_month=month, file_name=file_name)
        stored = reports.store_upload(
            db,
            parsed,
            report_month=month,
            file_name=file_name,
            uploaded_by=actor.id,
            replace=replace,
            sha256=digest,
            storage_key=storage_key,
        )
    except reports.DuplicateMonthError as duplicate:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(duplicate)) from None
    except CriticalityReportError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from None

    entry = next(e for e in reports.list_uploads(db, plant) if e.upload.id == stored.upload.id)
    return UploadResultResponse(
        upload=_entry(entry),
        stats=UploadStatsResponse(
            rows_read=parsed.rows_read,
            tiers=len(parsed.tiers),
            duplicates=parsed.duplicate_rows,
            blank_tiers=parsed.blank_tiers,
            skipped_blank_material=parsed.skipped_blank_material,
            file_plant=parsed.plant,
        ),
        is_current=stored.is_current,
        current_month=stored.current_month,
        replaced_upload_id=stored.replaced.id if stored.replaced else None,
        file_stored=storage_key is not None,
    )


__all__ = ["MAX_UPLOAD_BYTES", "router"]

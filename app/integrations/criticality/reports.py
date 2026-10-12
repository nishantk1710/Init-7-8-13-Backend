"""Monthly criticality reports: store an upload, and derive what is current.

Every initiative reads criticality from ``criticality_current`` -- I08 and I13
through the shared port (``app.integrations.criticality.zmm065``), I07's
staging and I08's ``v_zmm065`` view directly. This module is the only writer.

Which upload counts
-------------------
Per plant and month, the newest upload is *effective*; an earlier one for the
same month stays in history as *replaced* and contributes nothing. An upload
always beats the July delivery for the same month, whichever landed first --
the delivery is imported at start-up, and a restart must never undo somebody's
upload. The newest effective month is the plant's *current* report.

How the months combine (``rebuild_current``)
--------------------------------------------
Walking a plant's effective uploads newest month first:

* **Tier:** the newest *non-blank* tier wins. A material missing from August
  keeps July's tier, and so does one whose August cell is blank -- "not
  maintained this month" is not "no longer critical". ``carried_forward``
  marks every such tier, with the month it came from.
* **Description, material type, MRP type:** the newest non-blank value.

So an upload for an *older* month (June, after August) is allowed and only
fills materials no newer report lists.

The rebuild replaces the plant's rows whole, inside the upload's own
transaction: readers see the previous state or the new one, never half of each,
and running it again changes nothing.

Telling the snapshots
---------------------
Each stored upload also writes an ``ingestion_run`` row for
``criticality_current``. The I08 and I13 snapshot fingerprints and I07's source
fingerprint list that table, so an upload is a source change to all three
without any of them knowing this module exists.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import IO, Literal

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.integrations.criticality.report_parser import CriticalityReportError, ParsedReport
from app.models.criticality_report import (
    SOURCE_JULY_DELIVERY,
    SOURCE_UPLOAD,
    CriticalityCurrent,
    CriticalityReportRow,
    CriticalityReportUpload,
)
from app.models.ingestion import IngestionRun
from app.shared.plant_scope import IN_SCOPE_PLANTS

logger = get_logger(__name__)

#: The table every reader reads, and the name the source fingerprints watch.
CURRENT_TABLE = CriticalityCurrent.__tablename__

#: Rows per INSERT statement.
INSERT_BATCH = 2000

#: Storage prefix for the original files.
STORAGE_PREFIX = "criticality-reports"

Status = Literal["current", "previous", "replaced"]


class DuplicateMonthError(CriticalityReportError):
    """This plant already has a report for this month."""

    def __init__(self, existing: CriticalityReportUpload) -> None:
        self.existing = existing
        who = existing.uploaded_by or "the July delivery"
        when = f" on {existing.uploaded_at:%d %b %Y}" if existing.uploaded_at else ""
        super().__init__(
            f"Plant {existing.plant} already has a report for {existing.report_month:%B %Y} "
            f"({existing.file_name}, uploaded by {who}{when}). Replace it to make this file the "
            "current one for that month; the earlier file stays in history."
        )


@dataclass(frozen=True)
class StoredUpload:
    upload: CriticalityReportUpload
    #: The upload this one superseded for the same plant and month, if any.
    replaced: CriticalityReportUpload | None
    #: Whether this upload's month is now the plant's current report.
    is_current: bool
    #: The plant's current report month after this upload.
    current_month: date


@dataclass(frozen=True)
class UploadEntry:
    upload: CriticalityReportUpload
    status: Status


# --- ordering -------------------------------------------------------------


def _rank(upload: CriticalityReportUpload) -> tuple:
    """Sort key, best first: newest month, a real upload over the delivery,
    then newest upload."""
    uploaded_at = upload.uploaded_at
    stamp = uploaded_at.timestamp() if uploaded_at is not None and uploaded_at.tzinfo else (
        uploaded_at.replace(tzinfo=timezone.utc).timestamp() if uploaded_at is not None else 0.0
    )
    return (
        -upload.report_month.toordinal(),
        0 if upload.source == SOURCE_UPLOAD else 1,
        -stamp,
        -(upload.id or 0),
    )


def classify(uploads: Iterable[CriticalityReportUpload]) -> list[UploadEntry]:
    """Every upload with its status, best first per :func:`_rank`.

    Per plant: the first upload seen for a month is effective, the rest of that
    month are replaced; the first effective one is current.
    """
    entries: list[UploadEntry] = []
    seen_months: set[tuple[str, date]] = set()
    current_plants: set[str] = set()
    for upload in sorted(uploads, key=_rank):
        month_key = (upload.plant, upload.report_month)
        if month_key in seen_months:
            entries.append(UploadEntry(upload, "replaced"))
            continue
        seen_months.add(month_key)
        if upload.plant in current_plants:
            entries.append(UploadEntry(upload, "previous"))
        else:
            current_plants.add(upload.plant)
            entries.append(UploadEntry(upload, "current"))
    return entries


def list_uploads(session: Session, plant: str | None = None) -> list[UploadEntry]:
    """History, newest month first, with each upload's status."""
    query = select(CriticalityReportUpload)
    if plant is not None:
        query = query.where(CriticalityReportUpload.plant == plant)
    return classify(session.scalars(query))


def effective_uploads(session: Session, plant: str) -> list[CriticalityReportUpload]:
    """The plant's uploads that count, newest month first."""
    return [entry.upload for entry in list_uploads(session, plant) if entry.status != "replaced"]


def current_by_plant(session: Session) -> dict[str, CriticalityReportUpload]:
    return {entry.upload.plant: entry.upload for entry in list_uploads(session) if entry.status == "current"}


def materials_by_plant(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(CriticalityCurrent.plant, func.count()).group_by(CriticalityCurrent.plant)
    ).all()
    return {plant: count for plant, count in rows}


# --- writing --------------------------------------------------------------


def store_upload(
    session: Session,
    parsed: ParsedReport,
    *,
    report_month: date,
    file_name: str,
    uploaded_by: str | None,
    replace: bool = False,
    sha256: str | None = None,
    storage_key: str | None = None,
    source: str = SOURCE_UPLOAD,
    dedupe_key: str | None = None,
    today: date | None = None,
) -> StoredUpload:
    """Persist one parsed report, rebuild the plant's current values, record
    the source change, and commit -- all or nothing."""
    plant = parsed.plant
    if plant not in IN_SCOPE_PLANTS:
        raise CriticalityReportError(
            f"Plant {plant} is not one of the platform's plants ({', '.join(IN_SCOPE_PLANTS)})."
        )
    today = today or datetime.now(timezone.utc).date()
    if report_month > today.replace(day=1):
        raise CriticalityReportError(
            f"{report_month:%B %Y} has not started yet; a report can only be for this month or earlier."
        )

    same_month = [
        upload
        for upload in effective_uploads(session, plant)
        if upload.report_month == report_month
    ]
    replaced = same_month[0] if same_month else None
    if replaced is not None and not replace:
        raise DuplicateMonthError(replaced)

    upload = CriticalityReportUpload(
        plant=plant,
        report_month=report_month,
        file_name=file_name[:255],
        sheet_name=parsed.sheet_name[:64],
        source=source,
        dedupe_key=(dedupe_key or f"{SOURCE_UPLOAD}:{uuid.uuid4().hex}")[:64],
        uploaded_by=uploaded_by[:128] if uploaded_by else None,
        uploaded_at=datetime.now(timezone.utc),
        sha256=sha256,
        storage_key=storage_key,
        row_count=len(parsed.rows),
        tier_count=len(parsed.tiers),
        duplicate_rows=parsed.duplicate_rows,
        blank_tiers=parsed.blank_tiers,
        skipped_blank_material=parsed.skipped_blank_material,
    )
    try:
        session.add(upload)
        session.flush()
        rows = [
            {
                "upload_id": upload.id,
                "material": row.material,
                "material_raw": row.material_raw,
                "plant": row.plant,
                "criticality": row.criticality,
                "description": row.description,
                "mattyp": row.mattyp,
                "mrp_type": row.mrp_type,
            }
            for row in parsed.rows
        ]
        for start in range(0, len(rows), INSERT_BATCH):
            session.execute(insert(CriticalityReportRow), rows[start : start + INSERT_BATCH])
        current_month = rebuild_current(session, plant)
        _record_source_change(session, upload)
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(upload)
    logger.info(
        "criticality report stored: plant=%s month=%s rows=%d source=%s replaced=%s",
        plant, report_month, len(parsed.rows), source, replaced.id if replaced else None,
    )
    return StoredUpload(
        upload=upload,
        replaced=replaced,
        is_current=current_month == report_month,
        current_month=current_month,
    )


def rebuild_current(session: Session, plant: str) -> date | None:
    """Replace ``plant``'s rows in ``criticality_current`` from its effective
    uploads. Returns the plant's current month, or ``None`` with no uploads.
    Does not commit."""
    uploads = effective_uploads(session, plant)
    session.execute(delete(CriticalityCurrent).where(CriticalityCurrent.plant == plant))
    if not uploads:
        return None
    current_month = uploads[0].report_month

    merged: dict[str, dict] = {}
    for upload in uploads:  # newest month first
        rows = session.execute(
            select(
                CriticalityReportRow.material,
                CriticalityReportRow.material_raw,
                CriticalityReportRow.criticality,
                CriticalityReportRow.description,
                CriticalityReportRow.mattyp,
                CriticalityReportRow.mrp_type,
            ).where(CriticalityReportRow.upload_id == upload.id)
        )
        for material, material_raw, criticality, description, mattyp, mrp_type in rows:
            entry = merged.get(material)
            if entry is None:
                merged[material] = {
                    "material": material,
                    "material_raw": material_raw,
                    "plant": plant,
                    "criticality": criticality,
                    "description": description,
                    "mattyp": mattyp,
                    "mrp_type": mrp_type,
                    "tier_upload_id": upload.id if criticality else None,
                    "tier_report_month": upload.report_month if criticality else None,
                    "listed_upload_id": upload.id,
                    "listed_report_month": upload.report_month,
                }
                continue
            if entry["criticality"] is None and criticality:
                entry["criticality"] = criticality
                entry["tier_upload_id"] = upload.id
                entry["tier_report_month"] = upload.report_month
            for column, value in (("description", description), ("mattyp", mattyp), ("mrp_type", mrp_type)):
                if entry[column] is None and value:
                    entry[column] = value

    values = list(merged.values())
    for entry in values:
        tier_month = entry["tier_report_month"]
        entry["carried_forward"] = tier_month is not None and tier_month < current_month
    for start in range(0, len(values), INSERT_BATCH):
        session.execute(insert(CriticalityCurrent), values[start : start + INSERT_BATCH])
    return current_month


def _record_source_change(session: Session, upload: CriticalityReportUpload) -> None:
    """The ``ingestion_run`` row the snapshot fingerprints watch."""
    now = datetime.now(timezone.utc)
    session.add(
        IngestionRun(
            # Prefixed so it is never read as an ``odata:`` delta or a ``csv:`` load.
            source_file=f"criticality:{upload.file_name}"[:255],
            target_table=CURRENT_TABLE,
            row_count=upload.row_count,
            source_sha256=upload.sha256,
            status="succeeded",
            started_at=now,
            finished_at=now,
        )
    )


# --- the original file ----------------------------------------------------


def sha256_of(handle: IO[bytes]) -> str:
    """Hash a seekable handle and rewind it."""
    digest = hashlib.sha256()
    handle.seek(0)
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    handle.seek(0)
    return digest.hexdigest()


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def keep_original(handle: IO[bytes], *, plant: str, report_month: date, file_name: str) -> str | None:
    """Copy the uploaded file to storage and return its key, or ``None`` when
    storage is not configured or the write fails.

    Best effort on purpose: the parsed rows are the record every initiative
    reads, and refusing a valid report because the archive copy failed would
    hold up a month's criticality for a backup. The failure is logged.
    """
    if not get_settings().storage_url:
        return None
    from app.core.storage import get_storage

    safe_name = _UNSAFE.sub("_", file_name).strip("._") or "report.xlsx"
    key = f"{STORAGE_PREFIX}/{plant}/{report_month:%Y-%m}/{uuid.uuid4().hex[:12]}-{safe_name}"
    try:
        handle.seek(0)
        with get_storage().open_write(key) as target:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                target.write(chunk)
        return key
    except Exception as exc:  # noqa: BLE001 -- see the docstring
        logger.warning("criticality report %s could not be kept in storage (%s)", file_name, exc)
        return None
    finally:
        handle.seek(0)


__all__ = [
    "CURRENT_TABLE",
    "SOURCE_JULY_DELIVERY",
    "DuplicateMonthError",
    "StoredUpload",
    "UploadEntry",
    "classify",
    "current_by_plant",
    "effective_uploads",
    "keep_original",
    "list_uploads",
    "materials_by_plant",
    "rebuild_current",
    "sha256_of",
    "store_upload",
]

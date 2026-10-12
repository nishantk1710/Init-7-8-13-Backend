"""Monthly criticality reports (ZMM065), uploaded per plant.

Criticality used to come only from the July workbooks seeded into
``raw_zmm065_bmm`` / ``raw_zmm065_gb``. The report is reissued every month, so
each site's file is now uploaded on the Inventory Planning "Criticality
reports" page and lands here. The July delivery is imported once as each
plant's first report (``source='july_delivery'``), so every reader has one
place to look.

``criticality_report_upload``
    One row per file: which plant, which month, who uploaded it and when.
    Append-only -- a second file for the same plant and month is a new row,
    and the newer one is the one that counts for that month.

``criticality_report_row``
    Every parsed row of every upload, exactly as the file stated it (tier
    already parsed). Append-only: the record of what each month's report said.

``criticality_current``
    Derived: one row per material-plant, the values every initiative reads
    today. Rebuilt whole for a plant whenever one of its uploads lands (see
    ``app.integrations.criticality.reports.rebuild_current``), so it is safe to
    rebuild at any time. Each tier carries the upload and month it came from,
    because a material missing from the newest report keeps the last tier it
    had -- and that has to be visible, not silent.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: ``CriticalityReportUpload.source`` values.
SOURCE_UPLOAD = "upload"
SOURCE_JULY_DELIVERY = "july_delivery"


class CriticalityReportUpload(Base):
    __tablename__ = "criticality_report_upload"
    __table_args__ = (Index("ix_criticality_report_upload_plant_month", "plant", "report_month"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plant: Mapped[str] = mapped_column(String(10))
    #: First day of the month the report is for, as the uploader stated it.
    report_month: Mapped[date] = mapped_column(Date)
    file_name: Mapped[str] = mapped_column(String(255))
    #: The sheet the data was found on -- it moves between deliveries.
    sheet_name: Mapped[str] = mapped_column(String(64))
    #: ``upload`` or ``july_delivery``.
    source: Mapped[str] = mapped_column(String(32))
    #: Unique per logical upload: ``upload:<uuid>`` or ``july_delivery:<plant>``.
    #: The latter is what keeps the baseline import idempotent across restarts
    #: and instances.
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    #: ``None`` for the July delivery, which nobody uploaded.
    uploaded_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Where the original file was kept, or ``None`` when storage is not configured.
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

    row_count: Mapped[int] = mapped_column(Integer)
    tier_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_rows: Mapped[int] = mapped_column(Integer, default=0)
    blank_tiers: Mapped[int] = mapped_column(Integer, default=0)
    skipped_blank_material: Mapped[int] = mapped_column(Integer, default=0)


class CriticalityReportRow(Base):
    __tablename__ = "criticality_report_row"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    upload_id: Mapped[int] = mapped_column(ForeignKey("criticality_report_upload.id"), index=True)
    #: Leading zeros stripped and trimmed -- the form the ``n_*`` views and
    #: ``sap_key`` use, so a padded and an unpadded file join the same way.
    material: Mapped[str] = mapped_column(String(40))
    #: What the file said, before stripping.
    material_raw: Mapped[str] = mapped_column(String(40))
    plant: Mapped[str] = mapped_column(String(10))
    #: One of the five tiers, or ``None`` when the cell was blank.
    criticality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    mattyp: Mapped[str | None] = mapped_column(String(10), nullable=True)
    mrp_type: Mapped[str | None] = mapped_column(String(10), nullable=True)


class CriticalityCurrent(Base):
    __tablename__ = "criticality_current"
    __table_args__ = (UniqueConstraint("material", "plant", name="uq_criticality_current_material_plant"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    material: Mapped[str] = mapped_column(String(40))
    material_raw: Mapped[str] = mapped_column(String(40))
    plant: Mapped[str] = mapped_column(String(10), index=True)
    #: The newest non-blank tier any effective report gave, or ``None``.
    criticality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    mattyp: Mapped[str | None] = mapped_column(String(10), nullable=True)
    mrp_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    #: The upload and month the tier came from.
    tier_upload_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tier_report_month: Mapped[date | None] = mapped_column(Date, nullable=True)
    #: The upload and month that last listed the material at all.
    listed_upload_id: Mapped[int] = mapped_column(Integer)
    listed_report_month: Mapped[date] = mapped_column(Date)
    #: True when the tier is older than the plant's current report -- the
    #: material was missing, or blank, in the newest one.
    carried_forward: Mapped[bool] = mapped_column(Boolean, default=False)

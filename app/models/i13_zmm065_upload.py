"""ZMM065 monthly reports uploaded through the platform (I13 FR-6 validation).

ZMM065 is a SAP *report*, not a table, so no extract route carries it. The July
workbooks reached ``raw_zmm065_bmm`` / ``raw_zmm065_gb`` through the seed
loader; from then on VZI uploads each month's report on the Validation screen
and it lands here instead.

``i13_zmm065_upload``
    One row per uploaded workbook: which plant it covers, which month it is,
    who uploaded it and when. A re-upload for the same plant and month is a new
    row, never an overwrite -- the latest upload for a month is the one
    validation reads, and the earlier one stays as the record of what was
    reconciled before.

``i13_zmm065_upload_row``
    The rows of one upload that validation reads, already typed. Only the
    aging columns are kept: these uploads feed validation only. Criticality
    (I07, I08 and I13's reclassification indicator) still reads the seeded raw
    tables, so a monthly upload cannot change another initiative's answer.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Zmm065Upload(Base):
    __tablename__ = "i13_zmm065_upload"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plant: Mapped[str] = mapped_column(String(10), index=True)
    #: First day of the month the report is for, as the uploader stated it.
    report_month: Mapped[date] = mapped_column(Date, index=True)
    #: The run date the rows imply (``last_gi_dt + days``), or None if they don't.
    report_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    file_name: Mapped[str] = mapped_column(String(255))
    sheet_name: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer)
    uploaded_by: Mapped[str] = mapped_column(String(128))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Zmm065UploadRow(Base):
    __tablename__ = "i13_zmm065_upload_row"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    upload_id: Mapped[int] = mapped_column(ForeignKey("i13_zmm065_upload.id"), index=True)
    material: Mapped[str] = mapped_column(String(40))
    plant: Mapped[str] = mapped_column(String(10))
    stock_type: Mapped[str] = mapped_column(String(40))
    last_gi_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    days: Mapped[int | None] = mapped_column(Integer, nullable=True)

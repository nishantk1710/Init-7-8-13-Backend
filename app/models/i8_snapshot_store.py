"""The I08 snapshot, stored in Azure SQL (``I8_SNAPSHOT_STORE=sql``).

One row per build. A build process (``app/initiatives/i8/snapshot_store``)
inserts its row ``building``, assembles the snapshot, and writes it whole into
``payload`` -- gzip-compressed JSON, a few megabytes -- as it marks the row
``ready``. The API loads the newest ready row into memory and serves it; the
previous one is marked ``superseded`` and its payload cleared.

Survives a restart, so a restart is a load of a few seconds, not a rebuild:
before this, the snapshot lived only in the web worker, and every restart --
and every worker the build itself got killed in -- started it again from zero.

``fingerprint`` is what the I08 source tables looked like when the build
started (``service.source_state``): the API compares it with the current one
and starts a rebuild when they differ. ``codec_version`` guards the payload's
layout (``snapshot_store/codec.py``): a row written by another layout is never
decoded.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Integer, LargeBinary, Numeric, String, Unicode, UnicodeText
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class I8SnapshotRun(Base):
    __tablename__ = "i8_snapshot_run"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    # building | ready | failed | superseded
    status: Mapped[str] = mapped_column(String(16), index=True)
    reason: Mapped[str | None] = mapped_column(Unicode(80), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)
    codec_version: Mapped[int] = mapped_column(Integer)
    reference_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    build_seconds: Mapped[Decimal | None] = mapped_column(Numeric(12, 1), nullable=True)
    payload: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    payload_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)

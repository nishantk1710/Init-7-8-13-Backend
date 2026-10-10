"""The I13 snapshot, stored in Azure SQL (``I13_SNAPSHOT_STORE=sql``).

The in-memory snapshot (``app/initiatives/i13/snapshot.py``) holds every I13
result in the API process. On a production-sized extract -- 7.2M MSEG rows,
~65k OAR positions -- that no longer fits the App Service: the build is killed
for memory, the process restarts, and the next start-up build is killed again.
These two tables hold the same results in the database instead, written batch
by batch (``app/initiatives/i13/snapshot_store/builder.py``) and read with SQL
filters and paging (``.../reader.py``).

``i13_snapshot_run``
    One row per build. A build writes its records under its own ``version``
    and only becomes the one served once every batch has landed (status
    ``ready``); the previous version keeps serving until then and is deleted
    after. Survives a restart, so a restart no longer means a rebuild.

``i13_snapshot_record``
    One row per stored item, of a ``kind`` (``watch``, ``rledger`` ...). The
    item itself is ``payload``: a compact JSON array in the dataclass's field
    order (see ``snapshot_store/codec.py``). The columns beside it exist only
    to be filtered, sorted and paged on in SQL -- what each holds is per kind
    and documented in ``snapshot_store/kinds.py``.

The codec writes ASCII-only JSON (``ensure_ascii``), so ``payload`` is a plain
``VARCHAR``: half the bytes of ``NVARCHAR`` for the largest column here. It is
bounded (``PAYLOAD_MAX``) rather than ``MAX`` because pyodbc's
``fast_executemany`` -- what makes writing a few million rows take minutes, not
hours -- is unreliable with ``(MAX)`` parameters. The largest item, a 36-month
usage series, is about 1.5 KB; the writer refuses one that does not fit rather
than truncating it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Unicode,
    UnicodeText,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: Bytes. See the module docstring.
PAYLOAD_MAX = 8000


class I13SnapshotRun(Base):
    __tablename__ = "i13_snapshot_run"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    # building | ready | failed | superseded
    status: Mapped[str] = mapped_column(String(16), index=True)
    reason: Mapped[str | None] = mapped_column(Unicode(80), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Changes when a stored kind's field list does: a version written by older
    # code is never decoded by newer code (see codec.schema_signature).
    schema_signature: Mapped[str] = mapped_column(String(32))
    reference_date: Mapped[date] = mapped_column(Date)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    build_seconds: Mapped[Decimal | None] = mapped_column(Numeric(12, 1), nullable=True)
    batches: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Build-wide results that are not rows: chain diagnostics, reference-plan
    # count, history months, record counts. JSON.
    meta: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)
    error: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)


class I13SnapshotRecord(Base):
    __tablename__ = "i13_snapshot_record"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    # Write order within (version, kind): the order the in-memory snapshot
    # held these in, so a page reads the same rows either way.
    seq: Mapped[int] = mapped_column(BigInteger)
    material: Mapped[str] = mapped_column(Unicode(40))
    plant: Mapped[str] = mapped_column(Unicode(10))
    rec_key: Mapped[str | None] = mapped_column(Unicode(100), nullable=True)
    oar: Mapped[bool] = mapped_column(Boolean)
    f1: Mapped[str | None] = mapped_column(Unicode(40), nullable=True)
    f2: Mapped[str | None] = mapped_column(Unicode(40), nullable=True)
    f3: Mapped[str | None] = mapped_column(Unicode(40), nullable=True)
    n1: Mapped[Decimal | None] = mapped_column(Numeric(28, 6), nullable=True)
    d1: Mapped[date | None] = mapped_column(Date, nullable=True)
    text1: Mapped[str | None] = mapped_column(Unicode(400), nullable=True)
    payload: Mapped[str] = mapped_column(String(PAYLOAD_MAX))

    __table_args__ = (
        Index("ix_i13_snaprec_seq", "version", "kind", "seq"),
        Index("ix_i13_snaprec_key", "version", "kind", "material", "plant"),
        Index("ix_i13_snaprec_reckey", "version", "kind", "rec_key"),
        Index("ix_i13_snaprec_f1", "version", "kind", "f1"),
        Index("ix_i13_snaprec_f2", "version", "kind", "f2"),
        Index("ix_i13_snaprec_f3", "version", "kind", "f3"),
    )

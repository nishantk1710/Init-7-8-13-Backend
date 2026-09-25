"""One row per CSV extract request fired at SAP.

This table is the correlation mechanism, and it exists because the delivery
carries none of its own. SAP's push arrives as a bare CSV body: no request id,
no chunk number, no total, nothing tying a chunk to the request that asked for
it. The only fact the receiver can rely on is that exactly one extract is in
flight at a time -- which is true only because ``csv_pull`` refuses to fire a
second one while a row here is still OPEN.

So the sequence is: write the row, then fire. Never the other way round. A
chunk that arrives before its row exists cannot be attributed to anything, and
the receiver has to guess or drop it.

``expected_rows`` is captured BEFORE firing, from Interface 1's ``$count``.
Capturing it afterwards would race the extract itself on a live system.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Lifecycle. OPEN is the only state in which the receiver will attribute a
# chunk, and at most one row may hold it at a time.
STATUS_OPEN = "open"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"

TERMINAL_STATUSES = frozenset({STATUS_COMPLETE, STATUS_FAILED, STATUS_TIMEOUT})


class CsvExtractRequest(Base):
    """A fired ``TableExtractSet`` request and what came back for it."""

    __tablename__ = "csv_extract_request"

    request_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    """The RequestId sent in the entity key. Never reused -- SAP appears to
    dedupe on it, and a repeat fire silently delivers nothing."""

    sap_table: Mapped[str] = mapped_column(String(32), index=True)
    entity_set: Mapped[str] = mapped_column(String(128))

    from_date: Mapped[str] = mapped_column(String(8))
    to_date: Mapped[str] = mapped_column(String(8))
    max_rows: Mapped[str] = mapped_column(String(16), default="")

    status: Mapped[str] = mapped_column(String(16), index=True, default=STATUS_OPEN)

    reconcile: Mapped[str] = mapped_column(String(16), default="bounded")
    """EXACT for a wide window, BOUNDED for a three-year one. Decides whether
    ``received_rows`` must equal ``expected_rows`` or merely fall within it."""

    expected_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Interface 1's $count, taken before firing. None where that set cannot
    produce a trustworthy one -- several answer HTTP 500 to $count while
    serving rows perfectly well."""

    received_rows: Mapped[int] = mapped_column(Integer, default=0)
    received_chunks: Mapped[int] = mapped_column(Integer, default=0)
    received_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    """BigInteger, not Integer. CDPOS is 939,970 rows and this counter sums
    every chunk; INT tops out at 2.1 GB and would fail the insert, losing the
    accounting for the whole delivery along with it."""

    data_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """Storage key of the assembled file, set by the first chunk to land."""

    ack: Mapped[str | None] = mapped_column(String(512), nullable=True)
    """What /$value answered. 'Success: ... started in background chunks of
    50,000' means the request parsed -- it does NOT mean data will arrive."""

    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    last_chunk_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    loaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Set once csv_load has filed it. Null on a complete row means the rows
    are in storage and not yet in SQL."""

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"<CsvExtractRequest {self.request_id} {self.sap_table} "
            f"{self.status} {self.received_rows}/{self.expected_rows}>"
        )

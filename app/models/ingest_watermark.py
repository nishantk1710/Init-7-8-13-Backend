"""How far each entity set has been pulled.

``ingestion_run`` answers "what was loaded, from where, when". It cannot answer
"what do I ask SAP for next time", because that is a position in SAP's data
rather than a fact about a file -- the last change date seen on EKKO, say. One
row per entity set, holding that position.

Stored as text rather than a date. The watermark is whatever the delta field
carries, and OData hands those back as strings whose shape differs by
field -- ``Edm.DateTime`` arrives one way, a document number another. Parsing
it into a date here would force a guess about a format we do not control, and
the only thing this column has to do is come back out exactly as it went in.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class IngestWatermark(Base):
    """The high-water mark for one entity set's delta pulls."""

    __tablename__ = "ingest_watermark"

    # The entity set, not the table: the watermark is a position in SAP, and it
    # stays meaningful even if the target table is renamed.
    entity_set: Mapped[str] = mapped_column(String(128), primary_key=True)

    # Which property the mark refers to, e.g. "Aedat". Recorded because a
    # changed delta field invalidates the mark -- a date compared against a
    # position that was measured in document numbers is silently nonsense.
    field: Mapped[str] = mapped_column(String(64))

    # The highest value seen on the last successful pull.
    value: Mapped[str] = mapped_column(String(64))

    rows_last_run: Mapped[int] = mapped_column(Integer, default=0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<IngestWatermark {self.entity_set} {self.field}={self.value}>"

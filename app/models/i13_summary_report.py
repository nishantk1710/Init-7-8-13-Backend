"""I13 summary report -- persistence.

One table, ``i13_summary_report``: one row per generated report, never updated.
Regenerating writes a new row; the list shows the newest first. That keeps the
application's append-only rule (see ``tests/test_write_paths.py``) and means a
report someone forwarded last week still opens exactly as it was sent.

``report_json`` holds the facts and the narrative together (``Text``, not a
native JSON column, for the same portability reason as every other table here).
The provenance columns sit beside it so the list view and an audit can see who
generated a report, from which snapshot, and whether a model wrote the words,
without deserialising the payload.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class I13SummaryReport(Base):
    __tablename__ = "i13_summary_report"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    generated_by: Mapped[str] = mapped_column(String(120))

    plant: Mapped[str | None] = mapped_column(String(10), nullable=True)
    """``None`` means every plant."""

    snapshot_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)

    headline: Mapped[str] = mapped_column(String(600))

    narrative_source: Mapped[str] = mapped_column(String(16))
    """``MODEL`` or ``DETERMINISTIC`` -- who wrote the words."""
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    prompt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    report_json: Mapped[str] = mapped_column(Text)
    """``{"facts": ..., "narrative": ...}``."""

    def __repr__(self) -> str:
        return f"<I13SummaryReport {self.id} plant={self.plant or 'ALL'} source={self.narrative_source}>"

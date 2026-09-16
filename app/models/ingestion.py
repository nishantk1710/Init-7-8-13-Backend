"""Record of every extract file loaded into the database.

The seed script loads ~28 SAP extract workbooks, several of them split across
two files by Excel's row ceiling. Without a record of what was loaded, "is this
database current, and from which extract?" has no answer, and a partial or
repeated load is invisible.

This is foundation-level, not initiative-level: I07, I08 and I13 all read tables
this fills, so all three need to know when they were last filled.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class IngestionRun(Base):
    """One load of one source file into one target table."""

    __tablename__ = "ingestion_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # The extract file, as delivered -- e.g. "MSEG_1.XLSX". Not a path: paths
    # differ per machine, and the file name is what the SAP team refers to.
    source_file: Mapped[str] = mapped_column(String(255), index=True)

    # The table the rows landed in.
    target_table: Mapped[str] = mapped_column(String(128), index=True)

    row_count: Mapped[int] = mapped_column(Integer)

    # Content hash of the source file, so a re-run can tell "same file again"
    # from "the SAP team sent a new extract under the same name".
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(32))

    # Populated when status is a failure. Text, not String: tracebacks are long.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<IngestionRun {self.source_file} -> {self.target_table} "
            f"rows={self.row_count} status={self.status}>"
        )

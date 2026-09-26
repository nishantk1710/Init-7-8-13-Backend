"""I07 Quarterly Deep-Dive Report -- persistence.

One table, ``i7_quarterly_report``, holding the serialized
``QuarterlyReport`` (see ``app/schemas/i7/reports.py``) so a generated report
can be listed/retrieved later without recomputing.

**Idempotency decision: unique on ``quarter`` alone, upsert-by-quarter.**
Regenerating the same quarter overwrites that quarter's single row rather
than accumulating a new one -- the report is a read-side aggregation over
current table state (see the service module's docstring), not an immutable
event; a stale, superseded snapshot sitting alongside a fresh one under the
same quarter would be a trap for a caller who lists reports and picks the
"latest" without knowing two exist. ``report_version`` therefore is NOT part
of the uniqueness constraint -- it is a plain metadata column (the schema
version of the JSON payload in ``report_json``, e.g. ``"1.0"``, taken as-is
from ``REPORT_VERSION`` in the service module), bumped only when the report
*shape* changes, never per-generation. The repository's ``save_report``
implements the overwrite with a plain get-then-update-or-insert (see
``app/initiatives/i7/reporting/repository.py``) rather than a database-level
``ON CONFLICT`` upsert, which is a Postgres-only construct the portability
rule (no ``JSONB``/``ARRAY``/``ON CONFLICT``) rules out here too.

Portable constructs only: ``report_json`` is ``Text``, not a native JSON/JSONB
column -- same reasoning as every other table in this package.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class QuarterlyReportRecord(Base):
    """One quarter's I07 Quarterly Deep-Dive Report.

    ``status`` tracks the generation lifecycle (PENDING/RUNNING/COMPLETED/
    FAILED) so a caller can distinguish "still generating" or "last attempt
    failed" from a genuinely missing quarter -- ``report_json`` is only
    trustworthy to deserialize when ``status`` is COMPLETED; ``error`` is
    populated only on FAILED.

    The four ``*_run_id`` columns are the build-run references (feature/
    forecast/inventory/OAR) the report's figures were generated from, for
    traceability -- mirrors ``ReportMetadata``'s own fields in the schema,
    persisted alongside rather than only inside the JSON blob so a list view
    can show provenance without deserializing the payload.
    """

    __tablename__ = "i7_quarterly_report"
    __table_args__ = (
        UniqueConstraint("quarter", name="uq_i7_quarterly_report_quarter"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    quarter: Mapped[str] = mapped_column(String(16), index=True)
    """E.g. ``"Q3 2026"`` -- see ``resolve_quarter``. Unique alone: see the
    module docstring for why regeneration overwrites rather than
    accumulates."""

    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    status: Mapped[str] = mapped_column(String(32), index=True)
    """PENDING / RUNNING / COMPLETED / FAILED."""

    report_version: Mapped[str] = mapped_column(String(16))
    """The JSON payload's schema version (``QuarterlyReport``'s
    ``REPORT_VERSION``, e.g. ``"1.0"``) -- NOT part of the uniqueness
    constraint; see the module docstring."""

    report_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The full ``QuarterlyReport`` serialized as JSON text (``Text``, not
    JSONB, per the portability rule). ``None`` while ``status`` is PENDING/
    RUNNING/FAILED."""

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Populated only when ``status`` is FAILED."""

    feature_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forecast_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inventory_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    oar_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<QuarterlyReportRecord {self.quarter} status={self.status}>"

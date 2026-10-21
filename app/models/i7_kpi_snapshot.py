"""Saved monthly KPI snapshots for the I07 Quarterly Report.

The report's trends and its quarter-end position can only be honest if a month's figures were *saved when that month
was measured*: the stockout usage holds one build and no history, and recommendations are replaced by newer runs, so a
past month cannot be rebuilt afterwards. A snapshot is that saved record.

**One row per month** (unique on ``month``), so capturing the same month again never creates a second one:

* ``OPEN`` -- the month is still running. The row is a rolling reading, refreshed by each capture (each refresh is a
  new ``revision`` and is logged).
* ``FINAL`` -- captured once the month had ended. A final snapshot is never replaced silently: re-capturing is refused
  and logged unless it is forced, and a forced replacement is itself logged with both the old and the new fingerprint.

**What a snapshot speaks for.** ``cutoff`` is the day the figures speak for (the month's last day, or the capture day
while the month is open); ``measured_on`` is the day they were actually computed. ``month_end_valid`` says whether the
snapshot may stand in for the month-end position at all -- only a final one, measured within a month of the month's end.
The stockout reading carries its own flag, ``stockout_stale``, because it depends on a usage build whose date is
independent of the month: a snapshot can have valid excess and working-capital figures and a stale stockout reading.

**The population** is the newest recommendation of each material-plant generated on or before the month's end -- the
recommendations in force at that date -- so every month is judged over the same definition and months are comparable.

**Audit.** ``i7_kpi_snapshot_log`` is append-only: every create, update, finalisation, refusal, forced replacement and
failure, with the revision and the fingerprint of what was written. Nothing there is edited.

Portable constructs only (``Text`` for the payload, no JSON column), as the rest of this package.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

STATUS_OPEN = "OPEN"
STATUS_FINAL = "FINAL"

MONEY = Numeric(24, 4)


class KpiSnapshot(Base):
    __tablename__ = "i7_kpi_snapshot"
    __table_args__ = (UniqueConstraint("month", name="uq_i7_kpi_snapshot_month"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    month: Mapped[date] = mapped_column(Date, index=True)
    """First day of the month the snapshot is for."""
    status: Mapped[str] = mapped_column(String(8))
    """``OPEN`` or ``FINAL``."""

    cutoff: Mapped[date] = mapped_column(Date)
    measured_on: Mapped[date] = mapped_column(Date)
    month_end_valid: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- Where the source data stood when it was measured -------------------------------------
    latest_consumption_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    stockout_usage_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    stock_loaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- The headline figures, as columns so a trend can be read without parsing the payload --------
    population: Mapped[int] = mapped_column(Integer)
    stockout_at_risk: Mapped[int] = mapped_column(Integer)
    stockout_evaluated: Mapped[int] = mapped_column(Integer)
    stockout_insufficient: Mapped[int] = mapped_column(Integer)
    stockout_stale: Mapped[bool] = mapped_column(Boolean, default=True)
    """The usage was built too far from the cutoff to speak for it: the counts are kept, not trusted."""
    excess_count: Mapped[int] = mapped_column(Integer)
    excess_opportunity: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    net_rop_value_impact: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    priced_count: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    payload_json: Mapped[str] = mapped_column(Text)
    """Everything else, serialised: the per-plant positions and the current-vs-recommended stock levels."""
    payload_sha256: Mapped[str] = mapped_column(String(64))
    """Fingerprint of the figures (not of when they were measured): two captures with the same figures have the same
    fingerprint, which is what makes a repeat capture a no-op."""

    calc_version: Mapped[str] = mapped_column(String(16))
    """The version of the calculation that produced the figures (``snapshots.CALC_VERSION``)."""
    revision: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<KpiSnapshot {self.month:%Y-%m} {self.status} r{self.revision}>"


class KpiSnapshotLog(Base):
    """Append-only: one row per thing that happened to a snapshot."""

    __tablename__ = "i7_kpi_snapshot_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    month: Mapped[date] = mapped_column(Date, index=True)
    action: Mapped[str] = mapped_column(String(24))
    """CREATED / UPDATED / FINALISED / UNCHANGED / REFUSED / REPLACED_FINAL / FAILED."""
    revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

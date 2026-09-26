"""W6.3: the WATCH utilisation mart -- persisted, query-friendly serving
table for one material+plant's months-of-cover, acquired-vs-plan, 30-day
goods-received-not-issued, and W3.5 aging metrics.

Derived, initiative-specific table (see ``app/models/base.py``'s module
docstring): the raw SAP extracts it is built from live in the shared
``app/models``/``raw_*`` layer; this table belongs to I13 alone. Populated
only by ``app.initiatives.i13.watch_mart.refresh_watch_metrics_mart`` --
never written to directly, and never read by any other initiative today.

Grain is (material, plant), the preferred WATCH grain (FRS §5) -- acquisition
/reservation-level detail already has its own serving shape, the existing
``GET /api/i13/utilisation-ledger`` (W6.2's ``ReservationLedgerEntry``), so it
is not duplicated here.

Portable constructs only (no ``JSONB``/``ARRAY``/dialect-specific types) --
this table must create and query identically on Postgres (local dev) and
Azure SQL (the deployed target). Refresh is a portable delete-then-insert
(see ``watch_mart.py``), not an ``ON CONFLICT``/``MERGE`` upsert, for the same
reason.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Portable fixed-point type for every SAP-derived quantity/amount column.
# 18 integer digits, 6 fractional -- comfortably beyond any real SAP quantity
# or percentage this mart stores, on both Postgres NUMERIC and SQL Server
# DECIMAL.
_QTY = Numeric(18, 6)


class WatchMetricMart(Base):
    """One row per (material, plant) -- the full row shape is documented on
    ``app.initiatives.i13.models.WatchMetric``, which this table mirrors
    field-for-field; see that dataclass for what each column means."""

    __tablename__ = "i13_watch_metric_mart"

    material: Mapped[str] = mapped_column(String(40), primary_key=True)
    plant: Mapped[str] = mapped_column(String(10), primary_key=True)
    material_scope: Mapped[str] = mapped_column(String(16), index=True)

    stock_on_hand: Mapped[Decimal | None] = mapped_column(_QTY, nullable=True)
    open_po_quantity: Mapped[Decimal] = mapped_column(_QTY)
    average_monthly_consumption: Mapped[Decimal] = mapped_column(_QTY)
    months_of_cover: Mapped[Decimal | None] = mapped_column(_QTY, nullable=True)
    projected_months_of_cover: Mapped[Decimal | None] = mapped_column(_QTY, nullable=True)
    months_of_cover_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)

    last_movement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    days_since_last_movement: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    days_since_last_issue: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consumption_count_12m: Mapped[int] = mapped_column(Integer)
    consumed_qty_12m: Mapped[Decimal] = mapped_column(_QTY)
    inventory_turns: Mapped[Decimal | None] = mapped_column(_QTY, nullable=True)
    inventory_turns_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    aging_band: Mapped[str] = mapped_column(String(16), index=True)

    gr_not_issued_flag: Mapped[bool] = mapped_column(Boolean, index=True)
    gr_not_issued_days_since_gr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gr_not_issued_relevant_gr_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    gr_not_issued_threshold_days: Mapped[int] = mapped_column(Integer)
    gr_not_issued_received_quantity: Mapped[Decimal] = mapped_column(_QTY)
    gr_not_issued_issued_quantity: Mapped[Decimal] = mapped_column(_QTY)
    gr_not_issued_outstanding_quantity: Mapped[Decimal] = mapped_column(_QTY)

    acquired_vs_plan_status: Mapped[str] = mapped_column(String(16), index=True)
    planned_quantity: Mapped[Decimal | None] = mapped_column(_QTY, nullable=True)
    received_quantity: Mapped[Decimal] = mapped_column(_QTY)
    issued_quantity: Mapped[Decimal] = mapped_column(_QTY)
    acquired_vs_plan_variance_quantity: Mapped[Decimal | None] = mapped_column(_QTY, nullable=True)
    acquired_vs_plan_variance_percentage: Mapped[Decimal | None] = mapped_column(_QTY, nullable=True)

    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<WatchMetricMart {self.material}/{self.plant} scope={self.material_scope} band={self.aging_band}>"

"""I07 inventory calculation persistence.

Two tables: one per run, one per material-plant calculation. The traces are
stored as short delimited strings rather than JSON, for the same portability
reason as everywhere else -- no ``JSONB``, no ``ARRAY``.

A calculation is keyed on ``(run, material, plant)``, and a run is identified by
the feature, forecast, policy and formula versions that produced it. That is
what makes repetition idempotent: the same four versions produce the same run
key, and the unique constraint refuses a second copy.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

QUANTITY = Numeric(18, 6)


class InventoryRun(Base):
    """One execution of the inventory calculation engine."""

    __tablename__ = "i7_inventory_run"
    __table_args__ = (
        UniqueConstraint(
            "feature_run_id",
            "forecast_run_id",
            "policy_id",
            "policy_version",
            "formula_version",
            name="uq_i7_inventory_run_inputs",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    status: Mapped[str] = mapped_column(String(32), index=True)

    # The four inputs that determine the output. Together they are the run's
    # identity, which is what makes a repeat run idempotent rather than additive.
    feature_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forecast_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    policy_id: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[int] = mapped_column(Integer)
    formula_version: Mapped[str] = mapped_column(String(64))

    service_level_configured: Mapped[bool] = mapped_column(default=False)
    max_stock_strategy: Mapped[str | None] = mapped_column(String(32), nullable=True)

    calculations_written: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<InventoryRun {self.id} status={self.status} n={self.calculations_written}>"


class InventoryCalculation(Base):
    """Safety stock, ROP and maximum for one material-plant, with its trace."""

    __tablename__ = "i7_inventory_calculation"
    __table_args__ = (
        UniqueConstraint(
            "inventory_run_id",
            "sap_material_number",
            "sap_plant_code",
            name="uq_i7_inventory_calculation_key",
        ),
        Index(
            "ix_i7_inventory_calculation_material_plant",
            "sap_material_number",
            "sap_plant_code",
        ),
        Index("ix_i7_inventory_calculation_status", "safety_stock_status", "demand_class"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    inventory_run_id: Mapped[int] = mapped_column(Integer, index=True)

    sap_material_number: Mapped[str] = mapped_column(String(40))
    sap_plant_code: Mapped[str] = mapped_column(String(8))

    demand_class: Mapped[str] = mapped_column(String(32))
    selected_model: Mapped[str | None] = mapped_column(String(32), nullable=True)
    forecast_rate: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    forecast_unit: Mapped[str | None] = mapped_column(String(8), nullable=True)

    # --- Lead time ------------------------------------------------------
    lead_time_status: Mapped[str] = mapped_column(String(48), index=True)
    lead_time_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    po_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_po_count: Mapped[int] = mapped_column(Integer, default=0)
    excluded_cancelled_count: Mapped[int] = mapped_column(Integer, default=0)
    excluded_lt_error_count: Mapped[int] = mapped_column(Integer, default=0)
    outlier_count: Mapped[int] = mapped_column(Integer, default=0)
    lt_avg_days: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    lt_avg_months: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    sigma_lt_days: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    sigma_lt_months: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    planned_lt_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Demand variability (all periods, zeros included) ----------------
    variability_status: Mapped[str] = mapped_column(String(48))
    n_periods: Mapped[int] = mapped_column(Integer, default=0)
    zero_period_count: Mapped[int] = mapped_column(Integer, default=0)
    d_avg: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    sigma_d: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)

    # --- Service level ----------------------------------------------------
    service_level_status: Mapped[str] = mapped_column(String(48), index=True)
    service_level: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    z_factor: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    criticality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    circuit: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # --- Safety stock ------------------------------------------------------
    safety_stock_status: Mapped[str] = mapped_column(String(48), index=True)
    safety_stock_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw_safety_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    safety_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    safety_stock_trace: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # --- Reorder point ------------------------------------------------------
    rop_status: Mapped[str] = mapped_column(String(48), index=True)
    expected_lead_time_demand: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    raw_rop: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    rop: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rop_trace: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # --- Maximum stock -------------------------------------------------------
    max_stock_status: Mapped[str] = mapped_column(String(48), index=True)
    max_stock_strategy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    raw_max_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    max_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_stock_trace: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<InventoryCalculation {self.sap_material_number}/{self.sap_plant_code} "
            f"ss={self.safety_stock_status} rop={self.rop_status}>"
        )

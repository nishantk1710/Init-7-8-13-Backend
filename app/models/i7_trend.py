"""History of the Overview KPIs behind the two trend cards.

One row per **recommendation-generation run** per **plant scope**, written once when a
generation finishes successfully and never changed afterwards. It exists because the
Overview KPIs are computed live from "the newest recommendation per material-plant" and the
current stock, so the day after a run the previous day's figure is gone.

Two kinds of row, told apart by ``source``:

* ``captured``      written at the end of a generation, from the same calculation the
                    Overview uses, so every column is filled -- including the stockout
                    count, which needs the on-hand stock *at that moment*.
* ``reconstructed`` rebuilt afterwards from the stored recommendation rows. Only the excess
                    figures can be: on-hand stock is overwritten at every staging run and is
                    not stored on a recommendation, so ``stockout_risk_count`` is NULL here
                    -- never a guess, never zero.

Portable constructs only (no JSONB, no ARRAY), like every I07 model.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

SOURCE_CAPTURED = "captured"
SOURCE_RECONSTRUCTED = "reconstructed"

SCOPE_ALL = "ALL"
"""The whole population at once -- computed over every material-plant, not as the sum of the
plant rows. (A sum would be the same today but would silently double-count the moment a third
plant or a plant-less row existed.)"""

TREND_PLANT_SCOPES = (SCOPE_ALL, "1300", "1500")


class RecommendationTrendSnapshot(Base):
    """One generation's headline KPIs for one plant scope."""

    __tablename__ = "i7_recommendation_trend_snapshot"
    __table_args__ = (
        UniqueConstraint("generation_key", "plant_scope", name="uq_i7_trend_snapshot_generation_scope"),
        Index("ix_i7_trend_snapshot_scope_time", "plant_scope", "captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    generation_key: Mapped[str] = mapped_column(String(160))
    """Identity of the generation this describes, built from the upstream runs and the
    formula/policy it read (see ``trend_snapshots.generation_key``). A text key rather than one
    run id because no single column spans a generation: normal rows carry the inventory run,
    OAR rows the OAR run. It is what makes recording idempotent -- a timestamp would not."""

    plant_scope: Mapped[str] = mapped_column(String(8))
    """``ALL``, ``1300`` or ``1500``."""

    source: Mapped[str] = mapped_column(String(16))
    """``captured`` or ``reconstructed`` -- see the module docstring."""

    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    """When the figures describe: the moment the generation finished (captured), or when its
    rows were generated (reconstructed). This is the chart's x-axis."""

    inventory_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    oar_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forecast_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    feature_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    formula_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """Reference only. Nullable because zero would be a claim about run 0."""

    total_count: Mapped[int] = mapped_column(Integer)
    """Material-plants in scope (the newest recommendation of each)."""

    stockout_risk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Material-plants whose on-hand stock was below ``recommended_rop`` at ``captured_at``.
    NULL on a reconstructed row: the on-hand of that day is not stored anywhere."""

    excess_count: Mapped[int] = mapped_column(Integer)
    """Material-plants with ``current_max_stock > recommended_max_stock``."""

    excess_opportunity: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)
    """SUM(unit_price * (current_max_stock - recommended_max_stock)) over the excess rows that
    have a price. NULL when none does -- never defaulted to 0."""

    net_safety_stock_value_impact: Mapped[Decimal | None] = mapped_column(Numeric(20, 4), nullable=True)

    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    """The one currency of every priced material when they share one; NULL when they differ or
    none is priced -- the same rule the Overview applies. Amounts are never converted."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

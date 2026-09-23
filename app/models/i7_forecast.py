"""I07 forecasting persistence.

Four tables, each earning its place:

* ``i7_forecast_run``           one execution, with the policy that governed it
* ``i7_forecast``                the demand forecast per material-plant and model
* ``i7_segment_decision``        the champion/challenger audit log, one row per
                                 material-plant per run (despite the table's name --
                                 see below)
* ``i7_forecast_backtest_path``  one predicted/actual pair per rolling origin
                                 and horizon step -- see its own docstring for
                                 why this table exists despite the note below.

**Origin-by-origin paths were not persisted before 2026-09-22.** A full run
produces roughly 450 material-plants x 4 models x 10 origins of rows; until a
"Forecast vs Actual Demand" chart needed the real, per-period predicted/actual
history, nothing downstream read them beyond the in-memory aggregate metrics
computed at the end of each run. That reasoning no longer holds for the
champion model specifically -- see ``ForecastBacktestPath``. The champion's
paths are now persisted; every non-champion model's paths remain
unpersisted, for the same volume reason as before.

**Adoption grain moved from segment to material-plant.** The FRS is explicit,
twice, in Section 3.1 and FR-3: "Per-material model selection... the winning
model's recommendation is surfaced" and "select per material by backtest."
This table (and column, ``SegmentModelDecision.segment_key``) predate that
requirement and were built to a since-superseded Solution Design draft that
pooled adoption at demand-class grain ("Decision is per SEGMENT... not per
individual SKU"). Rather than adding a second, parallel table for what is the
same audit record at a different grain, this table's existing shape (one
adoption decision, its metrics, its reason) is reused unchanged --
``segment_key`` now holds ``"{material}/{plant}"`` instead of a demand-class
name. Kept the name rather than a migration-heavy rename: ``segment_key`` is
still, mechanically, "the key this decision was made at."

Portable constructs only: no ``JSONB``, no ``ARRAY``. Model parameters are
stored as a short delimited string rather than JSON for the same reason.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
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

RATE = Numeric(18, 6)
"""Demand rates and metrics are ratios; three decimals can hide a real
difference between two models."""


class ForecastRun(Base):
    """One execution of the forecasting service."""

    __tablename__ = "i7_forecast_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    status: Mapped[str] = mapped_column(String(32), index=True)

    policy_id: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[int] = mapped_column(Integer)
    """Which policy governed the run. The history gate, classification cutoffs
    and adoption thresholds all come from it."""

    feature_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """The feature-store build these forecasts were computed from."""

    target_quantile: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    """From the signed service-level matrix. NULL while unsigned, which is why
    LightGBM reports NOT_EVALUABLE_SERVICE_LEVEL_UNSET."""

    forecasts_written: Mapped[int] = mapped_column(Integer, default=0)
    materials_evaluated: Mapped[int] = mapped_column(Integer, default=0)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<ForecastRun {self.id} status={self.status} n={self.forecasts_written}>"


class Forecast(Base):
    """One model's demand forecast for one material-plant.

    Every model that ran is stored, not only the champion. A recommendation has
    to be able to show what the challenger predicted and why it was not adopted.
    """

    __tablename__ = "i7_forecast"
    __table_args__ = (
        UniqueConstraint(
            "forecast_run_id",
            "sap_material_number",
            "sap_plant_code",
            "model_name",
            name="uq_i7_forecast_key",
        ),
        Index("ix_i7_forecast_material_plant", "sap_material_number", "sap_plant_code"),
        Index("ix_i7_forecast_class_model", "demand_class", "model_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    forecast_run_id: Mapped[int] = mapped_column(Integer, index=True)

    sap_material_number: Mapped[str] = mapped_column(String(40))
    sap_plant_code: Mapped[str] = mapped_column(String(8))

    demand_class: Mapped[str] = mapped_column(String(32))
    history_status: Mapped[str] = mapped_column(String(32))

    model_name: Mapped[str] = mapped_column(String(32))
    model_version: Mapped[str] = mapped_column(String(64))
    """A deterministic implementation identifier (``sba-1``), not a semantic
    version -- nothing here maintains a release contract."""

    is_baseline: Mapped[bool] = mapped_column(default=False)
    """Whether this model is the demand class's *default* baseline (SES/SBA)
    before any per-material-plant swap -- SES/SBA is always True here,
    Auto-ARIMA/LightGBM/TSB always False. This is the model the class routes
    to by default, not necessarily the one actually selected for this
    material-plant; see ``is_champion`` for that."""

    is_champion: Mapped[bool] = mapped_column(default=False)
    """Whether this is the model actually selected for THIS material-plant,
    per its own backtest (FR-3: "select per material by backtest"). Exactly
    one row per material-plant/forecast-run has this set (the baseline,
    unless its own per-material decision is CHALLENGER_ELIGIBLE, in which
    case the challenger is champion instead) -- inventory/recommendations
    read this flag, not is_baseline, to find the forecast_rate to use."""

    adoption_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """This row's own per-material-plant AdoptionStatus (BASELINE_RETAINED /
    CHALLENGER_ELIGIBLE / NOT_ELIGIBLE_INSUFFICIENT_ORIGINS / NOT_EVALUABLE),
    from the same selection.decide_intermittent/decide_smooth rule applied at
    material-plant grain instead of segment grain. NULL on non-decision rows
    (TSB, and the row that was not itself compared as baseline/challenger)."""

    decision_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    """Why adoption_status is what it is -- the same reason text
    selection.decide_intermittent/decide_smooth already produce."""

    forecast_status: Mapped[str] = mapped_column(String(64), index=True)

    forecast_rate: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    """Units per month. NULL unless the status is SUCCESS -- an unavailable
    forecast is absent, never zero, because zero is itself a prediction."""

    forecast_unit: Mapped[str | None] = mapped_column(String(8), nullable=True)

    training_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    training_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    horizon_months: Mapped[int | None] = mapped_column(Integer, nullable=True)

    parameters: Mapped[str | None] = mapped_column(String(500), nullable=True)
    """Model metadata as ``key=value`` pairs joined by ``;`` -- SES alpha, ARIMA
    (p,d,q), SBA alpha/p_final/z_final. Only values actually computed appear."""

    backtest_status: Mapped[str] = mapped_column(String(64), index=True)
    required_origins: Mapped[int] = mapped_column(Integer, default=0)
    available_origins: Mapped[int] = mapped_column(Integer, default=0)
    origins_evaluated: Mapped[int] = mapped_column(Integer, default=0)

    pinball_loss: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    pinball_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mean_error: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    bias_percentage: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    mean_absolute_error: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    fill_rate: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    fill_rate_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    holding_cost: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    holding_cost_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SegmentModelDecision(Base):
    """The champion/challenger audit log, one row per material-plant per run.

    The FRS requires per-material-plant model selection by backtest (Section
    3.1, FR-3) -- which model won, by how much, over how many origins, and
    why, for THIS material-plant. ``segment_key`` holds ``"{material}/{plant}"``
    rather than a demand-class name; see the module docstring for why the
    table/column names were kept despite the grain change.
    """

    __tablename__ = "i7_segment_decision"
    __table_args__ = (
        UniqueConstraint(
            "forecast_run_id", "segment_key", name="uq_i7_segment_decision_key"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    forecast_run_id: Mapped[int] = mapped_column(Integer, index=True)
    segment_key: Mapped[str] = mapped_column(String(64), index=True)

    baseline_model: Mapped[str] = mapped_column(String(32))
    challenger_model: Mapped[str | None] = mapped_column(String(32), nullable=True)

    baseline_pinball: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    challenger_pinball: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    improvement: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)

    baseline_bias: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    challenger_bias: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)
    bias_change: Mapped[Decimal | None] = mapped_column(RATE, nullable=True)

    origins_available: Mapped[int] = mapped_column(Integer, default=0)
    origins_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    required_origins: Mapped[int] = mapped_column(Integer, default=0)

    adoption_status: Mapped[str] = mapped_column(String(64), index=True)
    decision_reason: Mapped[str] = mapped_column(String(500))

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<SegmentModelDecision {self.segment_key} {self.adoption_status}>"


class ForecastBacktestPath(Base):
    """One rolling-origin predicted/actual pair, for the CHAMPION model only.

    Added 2026-09-22 to back a real "Forecast vs Actual Demand" chart with
    the model's own historical predictions, rather than a client-side
    approximation computed from raw consumption alone (which is what the
    frontend's existing ForecastVsActualChart falls back to today when this
    table has nothing for a material-plant -- see that component).

    **Champion only, not every model.** Persisting every model's full path
    set (baseline + challenger, times every origin) would multiply row count
    several-fold over what the chart can ever show (it plots one series: what
    was actually recommended). ``backtest.run()`` already computes every
    model's ``BacktestResult.paths`` in memory regardless -- this table saves
    only the one path (``forecast_result.model == champion``) that
    corresponds to the forecast_rate actually feeding the recommendation,
    identified via ``Forecast.is_champion`` at write time in
    ``run_forecasting()``.

    ``predicted``/``actual`` reproduce ``OriginForecast`` exactly (see
    ``forecasting/types.py``) -- this table is that dataclass, persisted,
    nothing recomputed or reshaped.

    First data point exists only from the first forecast run generated after
    this table shipped; there is no way to reconstruct history for forecast
    runs that predate it, since the in-memory paths from those runs were
    already discarded.
    """

    __tablename__ = "i7_forecast_backtest_path"
    __table_args__ = (
        Index(
            "ix_i7_forecast_backtest_path_material_plant",
            "sap_material_number",
            "sap_plant_code",
        ),
        Index("ix_i7_forecast_backtest_path_run", "forecast_run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    forecast_run_id: Mapped[int] = mapped_column(Integer, index=True)
    sap_material_number: Mapped[str] = mapped_column(String(40))
    sap_plant_code: Mapped[str] = mapped_column(String(8))
    model_name: Mapped[str] = mapped_column(String(32))
    """The champion model's name for this material-plant on this run --
    denormalised from ``Forecast.model_name`` so a caller can read this table
    alone without a join, and so a model swap between runs is visible
    directly on the path rows themselves."""

    origin_period: Mapped[date] = mapped_column(Date)
    horizon_step: Mapped[int] = mapped_column(Integer)
    forecast_period: Mapped[date] = mapped_column(Date)
    """The month this specific prediction was FOR -- what the chart plots
    against, not ``origin_period`` (the month the model was standing at when
    it made the prediction)."""

    predicted: Mapped[Decimal] = mapped_column(RATE)
    actual: Mapped[Decimal] = mapped_column(RATE)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

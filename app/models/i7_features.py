"""I07 feature store.

    i7_staged_*  ->  feature builder  ->  i7_material_feature  ->  Phase 4

One row per material-plant. That grain is load-bearing: MRP type lives on MARC,
so a material can be OAR at one plant and planned at another, and collapsing to
material level would force an answer that does not exist.

**Stored, not derived on read.** ADI and CV-squared are cheap individually but
the pipeline reads them for tens of thousands of material-plants at a time, and
a recommendation must be able to show the exact figures it was built from months
later. Recomputing on every read would give neither.

**Every unavailable value is NULL with a status beside it.** ADI is undefined
when a material has no non-zero demand; CV-squared needs at least two non-zero
observations to have a standard deviation at all. Both are common. Storing 0 for
either would be a lie that classifies as SMOOTH -- the most confident,
lowest-safety-stock class -- so the column stays NULL and a status column says
why.

Portable constructs only; no ``JSONB``, no ``ARRAY``.
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

QUANTITY = Numeric(18, 3)
STATISTIC = Numeric(18, 6)
"""Wider scale than a quantity: ADI and CV-squared are ratios, and rounding one
to three places can move it across a classification cutoff."""


class FeatureBuildRun(Base):
    """One execution of the feature builder."""

    __tablename__ = "i7_feature_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    status: Mapped[str] = mapped_column(String(32), index=True)

    features_built: Mapped[int] = mapped_column(Integer, default=0)

    policy_id: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[int] = mapped_column(Integer)
    """Which policy produced these features. The ADI and CV-squared cutoffs, the
    history gate and the OAR rule all come from it, so a feature row cannot be
    interpreted without knowing which version was in force."""

    consumption_movement_types: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """Carried through from staging. The set is unconfirmed, so which definition
    of demand produced these numbers has to travel with them."""

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<FeatureBuildRun {self.id} status={self.status} built={self.features_built}>"


class MaterialFeature(Base):
    """Demand features, classification, routing and OAR scope for one
    material-plant."""

    __tablename__ = "i7_material_feature"
    __table_args__ = (
        UniqueConstraint(
            "sap_material_number", "sap_plant_code", name="uq_i7_material_feature_key"
        ),
        # Phase 4 selects by routing decision ("every intermittent material-plant
        # at this plant") far more often than by key.
        Index("ix_i7_material_feature_class", "demand_class", "sap_plant_code"),
        Index("ix_i7_material_feature_oar", "oar_scope", "sap_plant_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    sap_material_number: Mapped[str] = mapped_column(String(40), index=True)
    sap_plant_code: Mapped[str] = mapped_column(String(8), index=True)

    # --- History ------------------------------------------------------
    total_periods: Mapped[int] = mapped_column(Integer, default=0)
    """``n`` -- every month in the observed span, zeros included."""

    non_zero_periods: Mapped[int] = mapped_column(Integer, default=0)
    """``n_nz`` -- months with demand. Feeds ADI/CV-squared only; NOT the SOP
    3.1.1 consumption trigger -- see ``consumption_count_12m``."""

    consumption_count_12m: Mapped[int] = mapped_column(Integer, default=0)
    """MSEG issue transactions minus reversal transactions, trailing 12 months
    ending at the extract's own last staged month (:func:`observation_window`).
    A transaction-level event count, not a count of non-zero *months* --
    distinct from ``non_zero_periods``, which continues to feed ADI/CV-squared
    only. This is what the SOP 3.1.1 "more than four consumptions in the
    trailing twelve months" trigger reads."""

    first_period: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_period: Mapped[date | None] = mapped_column(Date, nullable=True)

    history_months: Mapped[int] = mapped_column(Integer, default=0)
    history_status: Mapped[str] = mapped_column(String(32), index=True)
    """SUFFICIENT / COLD_START / NO_HISTORY. A gating decision, not an error."""

    data_sufficiency: Mapped[str] = mapped_column(String(32))
    """How the available history compares with what the confidence thresholds
    ask for. The extract holds at most 13 months against a HIGH bar of 24, so
    this is mostly LIMITED -- recorded rather than worked around."""

    required_history_months: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Demand statistics --------------------------------------------
    total_demand: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)

    mean_demand_all_periods: Mapped[Decimal | None] = mapped_column(STATISTIC, nullable=True)
    """D_avg over ALL periods including zeros.

    Distinct from ``mean_non_zero_demand`` and not interchangeable with it. This
    one feeds the Phase 5 safety-stock formula; the other feeds CV-squared. The
    Formula Reference is explicit that sigma_D includes zeros while CV-squared
    excludes them, so both are stored separately."""

    std_dev_demand_all_periods: Mapped[Decimal | None] = mapped_column(
        STATISTIC, nullable=True
    )
    """sigma_D, zeros included. A Phase 5 input; no safety stock is computed here."""

    mean_non_zero_demand: Mapped[Decimal | None] = mapped_column(STATISTIC, nullable=True)
    """mu_nz -- CV-squared's denominator."""

    std_dev_non_zero_demand: Mapped[Decimal | None] = mapped_column(STATISTIC, nullable=True)
    """sigma_nz -- CV-squared's numerator."""

    # --- Classification ------------------------------------------------
    adi: Mapped[Decimal | None] = mapped_column(STATISTIC, nullable=True)
    """``n / n_nz``. NULL when n_nz is 0 -- undefined, never 0."""

    adi_status: Mapped[str] = mapped_column(String(64))
    """Wider than the other status columns: INSUFFICIENT_NON_ZERO_OBSERVATIONS
    is 34 characters."""

    cv_squared: Mapped[Decimal | None] = mapped_column(STATISTIC, nullable=True)
    """``(sigma_nz / mu_nz)^2``. NULL when fewer than two non-zero observations
    exist, or when mu_nz is 0."""

    cv_squared_status: Mapped[str] = mapped_column(String(64))

    demand_class: Mapped[str] = mapped_column(String(32), default="UNCLASSIFIED")
    """SMOOTH / ERRATIC / INTERMITTENT / LUMPY / UNCLASSIFIED."""

    # --- Routing --------------------------------------------------------
    baseline_model: Mapped[str | None] = mapped_column(String(32), nullable=True)
    challenger_model: Mapped[str | None] = mapped_column(String(32), nullable=True)
    routing_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """No champion is recorded. Champion selection needs Phase 4 backtesting."""

    # --- OAR scope -------------------------------------------------------
    oar_scope: Mapped[str] = mapped_column(String(32), index=True)
    """IN_SCOPE / OUT_OF_SCOPE / UNKNOWN, at material-plant grain."""

    oar_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """Which predicate decided it, so an UNKNOWN can be explained."""

    oar_rollup_status: Mapped[str] = mapped_column(String(32), default="NOT_CONFIGURED")
    """Material-level roll-up is an unresolved team-lead decision. Stays
    NOT_CONFIGURED rather than defaulting to the likely answer."""

    # --- Attribute availability -------------------------------------------
    criticality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """NULL means unknown. Never defaulted to NORMAL: criticality drives the
    service level, and a guess there produces a plausible wrong safety stock."""

    mrp_type: Mapped[str | None] = mapped_column(String(8), nullable=True)
    material_status: Mapped[str | None] = mapped_column(String(8), nullable=True)

    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    """MBEW moving average price (MOVING_PRICE), carried through from staging.
    Phase 5 reads unit price for economic-order-quantity and value-based
    filtering; stored here too so the feature store carries the full set of
    per-material-plant inputs rather than leaving price as a Phase-5-only read."""

    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    """Always NULL today: MBEW's extract carries no currency column. Left
    unset rather than assumed -- see StagedMaterial.currency's docstring."""

    has_unit_price: Mapped[bool] = mapped_column(default=False)

    has_criticality: Mapped[bool] = mapped_column(default=False)
    has_mrp_type: Mapped[bool] = mapped_column(default=False)
    has_material_status: Mapped[bool] = mapped_column(default=False)
    has_consumption: Mapped[bool] = mapped_column(default=False)
    has_lead_time: Mapped[bool] = mapped_column(default=False)
    """Whether any PO line for this material-plant yielded a usable duration.
    Phase 3 records availability only; the statistics are Phase 5's."""

    purchase_order_count: Mapped[int] = mapped_column(Integer, default=0)
    """PO lines with both dates present. Unfiltered -- the 1-730 day window is
    Phase 5 policy."""

    # --- MRP parameters (MARC, carried through from staging) --------------
    current_safety_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    """EISBE, as currently maintained in SAP. The Phase 7 benchmark compares a
    recommendation against this, not against zero."""

    current_reorder_point: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    """MINBE, as currently maintained in SAP."""

    current_maximum_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    """MABST, as currently maintained in SAP."""

    planned_delivery_time_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """PLIFZ. The MARC-PLIFZ interim lead-time fallback reads this same value --
    see :mod:`app.initiatives.i7.features.lead_time_provider`."""

    # --- Lead-time source (feature-selection level; see lead_time_provider) -
    lead_time_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """I11_PROGRAM / PLANNED_DELIVERY_TIME / CALCULATED -- which source answered
    :func:`app.initiatives.i7.features.lead_time_provider.resolve_lead_time`.
    Not Phase 5's PO-statistics method (ACTUAL_STATISTICAL etc.) -- a different,
    coarser axis: who supplied the figure, not how it was computed from POs."""

    lead_time_days: Mapped[Decimal | None] = mapped_column(STATISTIC, nullable=True)
    """The resolved lead time in days, from whichever source
    ``lead_time_source`` names. NULL when neither source has an answer."""

    # --- Stock position (MARD, summed across storage locations) -----------
    current_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    """Assumed primary stock-position figure: unrestricted-use stock (LABST),
    summed across a material-plant's storage locations. No document in this
    repository defines a stock-position formula -- this is a documented
    assumption (unrestricted-use only), not a discovered rule. NULL when the
    material-plant has no staged MARD rows, never 0."""

    quality_inspection_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    blocked_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    stock_in_transfer: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    restricted_use_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    returns_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    """Kept separate from ``current_stock`` rather than merged into it -- see
    ``current_stock``'s docstring."""

    has_stock_data: Mapped[bool] = mapped_column(default=False)
    """Whether any MARD row was staged for this material-plant."""

    storage_location_count: Mapped[int] = mapped_column(Integer, default=0)
    """How many storage locations contributed to the summed figures above."""

    # --- Provenance --------------------------------------------------------
    feature_run_id: Mapped[int] = mapped_column(Integer, index=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<MaterialFeature {self.sap_material_number}/{self.sap_plant_code} "
            f"class={self.demand_class} oar={self.oar_scope}>"
        )

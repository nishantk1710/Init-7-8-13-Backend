"""Thresholds the source documents DO specify.

Everything here has a documented value, so everything here has a default. That
is the opposite of :mod:`app.initiatives.i7.policy.unresolved`, where nothing
does and nothing may.

Defaults are the documented values, not opinions -- ADI 1.32 and CV-squared 0.49
are the Syntetos-Boylan cutoffs the Solution Design names. They are fields
rather than constants so calibration can move them without a code change, which
the FRS explicitly anticipates.

The confidence thresholds are kept exactly as documented even though the current
July/August data cannot satisfy HIGH: no material has the 24 months it requires.
Relaxing them to make the data look better would change what "high confidence"
means to an approver, which is a business decision and not ours.
"""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HistoryGatePolicy(BaseModel):
    """Stage 2b -- is there enough history to classify?"""

    model_config = ConfigDict(frozen=True)

    minimum_non_zero_periods: int = Field(default=5, ge=1)
    """Fewer than 5 non-zero demand periods routes to cold-start."""

    minimum_history_months: int = Field(default=6, ge=1)
    """Fewer than 6 months of history routes to cold-start."""


class ClassificationPolicy(BaseModel):
    """Stage 2c/2d -- ADI and CV-squared cutoffs.

    The formulas themselves (``ADI = n / n_nz``, ``CV2 = (sigma_nz / mu_nz)^2``)
    are arithmetic and belong in the classification module. Only the cutoffs are
    policy.
    """

    model_config = ConfigDict(frozen=True)

    adi_cutoff: float = Field(default=1.32, gt=0)
    """ADI <= cutoff is frequent demand."""

    cv_squared_cutoff: float = Field(default=0.49, gt=0)
    """CV-squared <= cutoff is stable quantity."""


class ModelAdoptionPolicy(BaseModel):
    """Stage 3 -- when a challenger may replace a baseline.

    A challenger never becomes champion automatically; it must clear both bars
    over the backtest.
    """

    model_config = ConfigDict(frozen=True)

    minimum_backtest_origins: int = Field(default=12, ge=1)
    """Rolling origins required. The current 12-month extract cannot supply
    this -- a data problem, recorded as such, not a reason to lower the bar."""

    minimum_pinball_improvement: float = Field(default=0.05, gt=0)
    """Fractional improvement in pinball loss required (>5%)."""

    maximum_bias_deterioration: float = Field(default=0.05, ge=0)
    """Bias may not worsen by more than this fraction (<=5%)."""


class SimilarityPolicy(BaseModel):
    """Stage 5 -- OAR cold-start similarity.

    Weights are the Solution Design's documented starting values, flagged there
    for empirical tuning.
    """

    model_config = ConfigDict(frozen=True)

    structural_weight: float = Field(default=0.35, ge=0, le=1)
    text_weight: float = Field(default=0.30, ge=0, le=1)
    business_weight: float = Field(default=0.35, ge=0, le=1)

    minimum_neighbours: int = Field(default=5, ge=1)
    """Hard admission gate: fewer than this many *qualifying* (>= minimum_similarity)
    neighbours blocks the estimate entirely -- see oar/estimate.py's
    NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS. Never relaxed by padding with
    low-similarity candidates."""

    maximum_neighbours: int = Field(default=10, ge=1)
    """Documented K range is 5-10."""

    minimum_similarity: float = Field(default=0.60, ge=0, le=1)
    """Hard admission gate: a candidate with combined similarity below this
    value may not contribute to the OAR SS/ROP/Max weighted estimate, however
    many candidates are available. Applied with >=, so exactly 0.60 qualifies.
    Distinct from ConfidencePolicy.oar_medium_minimum_similarity, which grades
    confidence on an already-admitted neighbour set rather than deciding
    admission."""

    require_same_criticality: bool = True
    """Hard constraint: a neighbour must share the criticality class."""

    minimum_neighbour_history_months: int = Field(default=12, ge=1)
    """Hard constraint: a neighbour needs at least this much history."""

    @model_validator(mode="after")
    def _weights_and_k(self) -> "SimilarityPolicy":
        total = self.structural_weight + self.text_weight + self.business_weight
        # Float tolerance: 0.35 + 0.30 + 0.35 is not exactly 1.0 in binary.
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"similarity weights must sum to 1.0, got {total}")
        if self.minimum_neighbours > self.maximum_neighbours:
            raise ValueError("minimum_neighbours must not exceed maximum_neighbours")
        return self


class ConfidencePolicy(BaseModel):
    """Stage 6 -- confidence grade bands.

    Kept as documented. The July/August extract reaches at most 12 months, so
    nothing grades HIGH today; that is a finding about the data.
    """

    model_config = ConfigDict(frozen=True)

    high_minimum_history_months: int = Field(default=24, ge=1)
    high_minimum_purchase_orders: int = Field(default=5, ge=1)

    medium_minimum_history_months: int = Field(default=12, ge=1)
    medium_minimum_purchase_orders: int = Field(default=2, ge=1)

    oar_high_minimum_neighbours: int = Field(default=5, ge=1)
    oar_high_minimum_similarity: float = Field(default=0.80, ge=0, le=1)
    oar_medium_minimum_neighbours: int = Field(default=3, ge=1)
    oar_medium_minimum_similarity: float = Field(default=0.60, ge=0, le=1)

    @model_validator(mode="after")
    def _bands_ordered(self) -> "ConfidencePolicy":
        if self.medium_minimum_history_months > self.high_minimum_history_months:
            raise ValueError("MEDIUM history threshold must not exceed HIGH")
        if self.medium_minimum_purchase_orders > self.high_minimum_purchase_orders:
            raise ValueError("MEDIUM purchase-order threshold must not exceed HIGH")
        if self.oar_medium_minimum_similarity > self.oar_high_minimum_similarity:
            raise ValueError("MEDIUM similarity threshold must not exceed HIGH")
        if self.oar_medium_minimum_neighbours > self.oar_high_minimum_neighbours:
            raise ValueError("MEDIUM neighbour threshold must not exceed HIGH")
        return self


class LeadTimePolicy(BaseModel):
    """Stage 3 -- lead-time validity bounds and fallback tiers."""

    model_config = ConfigDict(frozen=True)

    minimum_valid_days: int = Field(default=1, ge=0)
    maximum_valid_days: int = Field(default=730, ge=1)
    """Outside 1-730 days is a data error or an outlier."""

    full_statistics_minimum_orders: int = Field(default=5, ge=1)
    """>=5 POs supports full statistical analysis."""

    warning_minimum_orders: int = Field(default=2, ge=1)
    """2-4 POs is usable but flagged WARNING; below that, LIMITED and the
    planned delivery time stands in."""

    planned_delivery_variability_factor: float = Field(default=0.30, ge=0)
    """Documented conservative default: sigma_LT = 0.3 x planned lead time when
    there is no PO history to measure."""

    days_per_month: float = Field(default=30.44, gt=0)
    """Mean Gregorian month, for the day-to-month conversion."""

    @model_validator(mode="after")
    def _bounds_ordered(self) -> "LeadTimePolicy":
        if self.minimum_valid_days >= self.maximum_valid_days:
            raise ValueError("minimum_valid_days must be below maximum_valid_days")
        if self.warning_minimum_orders > self.full_statistics_minimum_orders:
            raise ValueError("warning tier must not require more orders than the full tier")
        return self


class ConversionTriggerPolicy(BaseModel):
    """Stage 5A -- when an OAR material may be recommended for Min-Max.

    Triggers are OR-ed: any one suffices. Each is independently switchable
    because their wording is not fully settled -- the FRS says "Critical
    classification" in FR-5 but "Critical or of significant production impact"
    in section 3.1, which are different tier sets.
    """

    model_config = ConfigDict(frozen=True)

    consumption_count_months: int = Field(default=12, ge=1)
    consumption_count_threshold: int = Field(default=4, ge=0)
    """Trigger 1: more than 4 consumptions in a trailing 12 months."""

    enable_consumption_trigger: bool = True
    enable_criticality_trigger: bool = True
    enable_i13_hod_trigger: bool = True

    criticality_trigger_tiers: tuple[str, ...] | None = None
    """Trigger 2's tier set. **Unset**: the two FRS statements disagree on
    whether IMPACT counts alongside CRITICAL, and picking one would be inventing
    the answer. Phase 6 must raise
    :class:`~app.initiatives.i7.errors.PolicyNotConfiguredError` while this is
    ``None`` and the trigger is enabled."""


class AdoptionPolicy(BaseModel):
    """Stage 5A -- detecting that VZI executed an approved change in SAP.

    Read-only. I07 never writes to SAP; these fields describe what to look for
    in the change documents, not anything to send.
    """

    model_config = ConfigDict(frozen=True)

    monitoring_window_days: int | None = None
    """**Unset.** The FRS calls the window configurable and to be tuned during
    calibration, and never states a value."""

    converted_mrp_type: str = "VB"
    """Conversion is adopted when MRP type becomes VB with MINBE and MABST
    populated. ``VB`` is the one concrete MRP value the documents commit to."""

    tracked_fields: tuple[str, ...] = ("DISMM", "EISBE", "MINBE", "MABST")
    """The MARC fields FR-9 watches in CDPOS."""

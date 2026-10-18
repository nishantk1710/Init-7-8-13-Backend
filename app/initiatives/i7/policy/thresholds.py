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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.criticality import CriticalityTier


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


SIMILARITY_FEATURES = (
    "material_group",
    "equipment_type",
    "base_unit_of_measure",
    "manufacturer",
    "unit_price",
    "description",
)
"""The features an OAR similarity score can rest on. Must equal
``app.initiatives.i7.oar.types.FEATURE_NAMES`` -- duplicated rather than
imported because the OAR package imports this module, and a test pins the two
together. Circuit is deliberately absent: there is no source for it."""


class SimilarityPolicy(BaseModel):
    """Stage 5 -- OAR cold-start similarity.

    Weights are the Solution Design's documented starting values, flagged there
    for empirical tuning.
    """

    model_config = ConfigDict(frozen=True)

    structural_weight: float = Field(default=0.35, ge=0, le=1)
    text_weight: float = Field(default=0.30, ge=0, le=1)
    business_weight: float = Field(default=0.35, ge=0, le=1)

    minimum_neighbours: int = Field(default=3, ge=1)
    """Hard admission gate: fewer than this many *qualifying* (>= minimum_similarity)
    neighbours blocks the estimate entirely -- see oar/estimate.py's
    NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS. Never relaxed by padding with
    low-similarity candidates.

    3, set 2026-10-19 (was 5). The Solution Design and Formula Reference say
    K = 5-10; this is a deliberate business override, not a reading of them.
    Every neighbour now carries SAP values to lend (see
    ``oar.repository.load_candidate_population``), so the minimum counts
    materials that actually contribute to the estimate."""

    maximum_neighbours: int = Field(default=5, ge=1)
    """Top-K cap. 5, set 2026-10-19 (was 10; the documents say 5-10)."""

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

    minimum_evidence_features: int = Field(default=2, ge=0)
    """Admission gate (oar-similarity-2): a pair is admitted as a neighbour only
    if at least this many features were actually compared -- known on both
    sides. Renormalising over available features otherwise lets one feature
    alone produce a 1.0. 0 disables the count requirement."""

    identity_evidence_features: tuple[str, ...] = ("material_group", "description")
    """Admission gate (oar-similarity-2): at least one of these must have been
    compared. They say what a part IS; unit of measure and price only say how
    it is counted and what it costs, and thousands of unrelated parts share
    both. Empty disables the requirement."""

    @field_validator("identity_evidence_features")
    @classmethod
    def _known_features(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = [name for name in value if name not in SIMILARITY_FEATURES]
        if unknown:
            raise ValueError(
                f"unknown similarity feature(s) {unknown}; expected some of {SIMILARITY_FEATURES}"
            )
        return value

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


_KNOWN_CRITICALITY_TIERS = frozenset(tier.value for tier in CriticalityTier)


class ConversionTriggerPolicy(BaseModel):
    """Stage 5A -- when an OAR material may be recommended for Min-Max.

    Both conditions must hold (AND), and only for a material-plant Stage 1
    already classified as OAR:

    * more than ``consumption_count_threshold`` net issue transactions in the
      trailing 12 months, **and**
    * a criticality tier in ``criticality_trigger_tiers``.

    A tier in ``excluded_criticality_tiers`` is never eligible, whatever the
    other condition says. There is no third trigger: the I13 HOD-approved
    request was removed from the rule, not merely switched off.
    """

    model_config = ConfigDict(frozen=True)

    consumption_count_months: int = Field(default=12, ge=1)
    consumption_count_threshold: int = Field(default=4, ge=0)
    """More than 4 net issue transactions in a trailing 12 months."""

    criticality_trigger_tiers: tuple[str, ...] | None = None
    """The tiers that satisfy the criticality condition. **Unset** on the bare
    model, so a policy built without a decision cannot quietly pick one; while
    this is ``None`` the criticality condition evaluates UNKNOWN and
    ``PolicyDocument.unresolved_policies`` reports
    ``conversion_criticality_tiers``. The confirmed set lives in
    :func:`current_conversion_trigger_policy`."""

    excluded_criticality_tiers: tuple[str, ...] = (CriticalityTier.OBSOLETE.value,)
    """Tiers that are never eligible. OBSOLETE: a material being run down is
    not one to start stocking on Min-Max, however often it moved."""

    @field_validator("criticality_trigger_tiers", "excluded_criticality_tiers")
    @classmethod
    def _known_tiers(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        # A misspelt tier would match nothing and make every material
        # NOT_ELIGIBLE with no error anywhere, so refuse it at load.
        if value is None:
            return value
        unknown = [tier for tier in value if tier not in _KNOWN_CRITICALITY_TIERS]
        if unknown:
            raise ValueError(
                f"unknown criticality tier(s) {unknown}; expected some of "
                f"{sorted(_KNOWN_CRITICALITY_TIERS)}"
            )
        return value

    @model_validator(mode="after")
    def _excluded_tiers_never_qualify(self) -> "ConversionTriggerPolicy":
        overlap = set(self.criticality_trigger_tiers or ()) & set(self.excluded_criticality_tiers)
        if overlap:
            raise ValueError(
                f"criticality tier(s) {sorted(overlap)} cannot both qualify for and be "
                "excluded from Min-Max conversion"
            )
        return self


def current_conversion_trigger_policy() -> ConversionTriggerPolicy:
    """The rule in force: ``criticality_trigger_tiers=("CRITICAL", "IMPACT")``.

    Business-confirmed: an OAR material-plant is eligible for Min-Max only when
    it had more than 4 net issue transactions in the trailing 12 months **and**
    is CRITICAL or IMPACT. OBSOLETE is never eligible. This replaces the
    earlier rule (consumption OR a NORMAL tier OR an I13 HOD approval).

    Matches :func:`app.initiatives.i7.policy.oar.current_oar_policy`'s pattern:
    the resolved rule lives in one function a reviewer can find, rather than a
    default silently baked into the schema.
    """
    return ConversionTriggerPolicy(
        criticality_trigger_tiers=(CriticalityTier.CRITICAL.value, CriticalityTier.IMPACT.value),
    )


class DecliningDemandPolicy(BaseModel):
    """The declining-demand rule (v1): which materials' demand has been falling.

    Business thresholds, kept here so changing one is a policy edit and not a
    code change. The rule itself lives in
    :mod:`app.initiatives.i7.features.declining`. These values are a proposal
    that has not been validated on Vedanta history -- ``rule_version`` is stored
    with every evaluation so a later change can be told apart from this one.

    The window is split into two equal halves, earlier and recent. A material is
    flagged only when it had recurring demand in the earlier half, its months
    with demand at least halved in the recent half, and the most recent months
    are all empty.
    """

    model_config = ConfigDict(frozen=True)

    rule_version: str = "declining-v1"

    window_months: int = Field(default=12, ge=4)
    """Complete calendar months evaluated. Must be even: the window splits into
    an earlier and a recent half of ``window_months / 2`` each."""

    minimum_nonzero_months: int = Field(default=4, ge=1)
    """Months with demand needed across the whole window to evaluate at all."""

    minimum_early_nonzero_months: int = Field(default=3, ge=1)
    """Months with demand needed in the earlier half: recurring demand before
    the decline."""

    minimum_decline: float = Field(default=0.50, gt=0, le=1)
    """``1 - recent_rate / early_rate`` must reach this."""

    zero_latest_months: int = Field(default=3, ge=1)
    """The latest months that must all be empty."""

    @model_validator(mode="after")
    def _window_is_consistent(self) -> "DecliningDemandPolicy":
        if self.window_months % 2:
            raise ValueError("window_months must be even")
        half = self.window_months // 2
        if self.zero_latest_months > half:
            raise ValueError("zero_latest_months cannot exceed half the window")
        if self.minimum_early_nonzero_months > half:
            raise ValueError("minimum_early_nonzero_months cannot exceed half the window")
        if self.minimum_nonzero_months > self.window_months:
            raise ValueError("minimum_nonzero_months cannot exceed the window")
        return self


class ApprovalRoutingPolicy(BaseModel):
    """Which approval chain a ROP/Max recommendation follows, by criticality
    tier.

    Configuration, not API logic: a call site asks this policy for a tuple of
    role *values* and enforces it through the same ``workflow`` state machine
    every recommendation already uses -- it never branches on criticality
    itself. Roles are plain strings here (matching ``ApprovalRole``'s own
    string values) rather than the enum itself, to avoid a circular import
    between ``app.initiatives.i7.policy`` and
    ``app.initiatives.i7.recommendations``; ``recommendations.routing``
    converts them to ``ApprovalRole`` at the one seam that needs the enum.

    Defaults to the historical four-step chain's role values for every tier,
    so an unconfigured deployment behaves exactly as it did before this
    policy existed; a signed business decision to route CRITICAL materials
    through a shorter or different chain is a configuration edit here, not a
    code change.

    OAR conversion recommendations do not use this policy at all -- they
    always take the fixed OAR route required regardless of criticality (see
    ``recommendations.routing.route_for``).
    """

    model_config = ConfigDict(frozen=True)

    rop_max_route_by_tier: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    """Criticality tier value (e.g. ``"CRITICAL"``) -> its route, as role
    values. A tier absent from this mapping falls back to ``default_route``."""

    default_route: tuple[str, ...] = (
        "End User",
        "Engineering Manager",
        "Commercial Manager",
        "Warehouse Supervisor",
    )

    def route_for(self, criticality: str | None) -> tuple[str, ...]:
        """The configured route for this tier, or ``default_route`` if the
        tier is unset or has no specific override."""
        if criticality is None:
            return self.default_route
        return self.rop_max_route_by_tier.get(criticality, self.default_route)


class AdoptionPolicy(BaseModel):
    """Stage 5A -- detecting that VZI executed an approved change in SAP.

    Read-only. I07 never writes to SAP; these fields describe what to look for
    in the change documents, not anything to send.
    """

    model_config = ConfigDict(frozen=True)

    monitoring_window_days: int | None = 15
    """Default 15 days, per business direction (2026-09-22) -- the FRS calls
    the window configurable and to be tuned during calibration but never
    itself states a value, so this is a deliberate starting default, not a
    value drawn from the FRS. Still overridable per policy document; a future
    calibration exercise may confirm a different number without a code
    change."""

    converted_mrp_type: str = "VB"
    """Conversion is adopted when MRP type becomes VB with MINBE and MABST
    populated. ``VB`` is the one concrete MRP value the documents commit to."""

    tracked_fields: tuple[str, ...] = ("DISMM", "EISBE", "MINBE", "MABST")
    """The MARC fields FR-9 watches in CDPOS."""

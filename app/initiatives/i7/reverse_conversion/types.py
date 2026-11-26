"""Types of the Min-Max -> OAR recommendation feature.

Plain data, no behaviour and no database: the evaluator is a pure function of these.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class ReverseCategory(StrEnum):
    """What a material-plant is recommended as. The first two are the only categories a user sees.

    There is no separate Manual Review and no Obsolescence Review: a material a planner has to look at
    (including one classified OBSOLETE) stays in Recommended for OAR (shown to the user as Convert to OAR)
    and carries a :class:`ReverseConfidence` that says how sure the rule is."""

    RECOMMENDED_FOR_OAR = "Recommended for OAR"
    NOT_ELIGIBLE = "Not Eligible"

    EXCLUDED = "Excluded"
    """Outside the scope of the feature (MRP type, plant or deletion flag). Never listed or counted in
    a category; the service reports how many were left out."""


#: The categories a recommendation can carry, in display order.
FINAL_CATEGORIES: tuple[ReverseCategory, ...] = (
    ReverseCategory.RECOMMENDED_FOR_OAR,
    ReverseCategory.NOT_ELIGIBLE,
)


class ReverseConfidence(StrEnum):
    """How sure the rule is that a Convert to OAR material should be ordered on requirement. The value
    is the label shown to a user. Only Convert to OAR rows carry one."""

    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


#: Display and ranking order, most confident first.
CONFIDENCE_ORDER: tuple[ReverseConfidence, ...] = (
    ReverseConfidence.HIGH,
    ReverseConfidence.MEDIUM,
    ReverseConfidence.LOW,
)


class ReverseReason(StrEnum):
    """Why a material-plant got its category. The value is the label shown to a user."""

    OUTSIDE_SCOPE = "Outside scope"
    RECENTLY_CHANGED = "Recently changed planning type"
    """Moved between OAR and Min-Max within the cooldown: not recommended for conversion again yet."""

    UNKNOWN_CRITICALITY = "Unknown criticality"
    PROTECTED_CRITICALITY = "Protected criticality"
    OBSOLETE_CRITICALITY = "Obsolete"
    INCONSISTENT_MOVEMENT = "Inconsistent movement data"
    NEVER_MOVED = "Zero issues in 24 months and never moved"
    ZERO_USE_24M = "Zero issues in 24 months but moved before"
    INSUFFICIENT_HISTORY = "Insufficient history"
    LEAD_TIME = "Lead time unknown or above 90 days"
    LAPSED = "Zero issues in latest 12 complete months but used previously"
    FREQUENT_CONSUMPTION = "Frequent consumption"
    SPARSE_RECURRING_DEMAND = "Sparse recurring demand"

    # Rule 13: an active material that is none of the above.
    BORDERLINE_FREQUENCY = "Borderline frequency"
    STALE_LAST_MOVEMENT = "Stale last movement"
    LIFETIME_CRITERION = "Lifetime criterion"
    UNCLASSIFIED_ACTIVE = "Unclassified active material"
    """Defensive only: an active material that satisfies none of the three rule-13 reasons. It cannot
    happen with a consistent policy (a test proves it); it exists so the evaluator can never return
    without a reason."""


@dataclass(frozen=True)
class MonthlyMovement:
    """One month of one material-plant's consumption, as staged (``i7_staged_consumption``)."""

    period: date
    """Any day in the month; normalised to the first of the month."""

    net_issued_quantity: Decimal
    """Issues minus reversals, in the base unit. A month is a *valid movement* only when this is above zero."""

    issue_count: int
    reversal_count: int


@dataclass(frozen=True)
class ConsumptionMetrics:
    """Consumption of one material-plant measured up to the anchor. Everything the rule reads."""

    anchor: date
    """First day of the latest complete month the metrics are measured to."""

    issues_latest_window: int
    """Net issue transactions (issues minus reversals, floored at zero) in the latest 12 months."""

    months_with_use_latest_window: int
    """Months in the latest 12 with a valid movement."""

    issues_prior_window: int
    """The same count for the 12 months before that."""

    months_with_use_prior_window: int

    first_valid_month: date | None
    last_valid_month: date | None
    """First and latest month with a valid movement, up to the anchor. None when there is none."""

    lifetime_months_with_use: int
    """Months with a valid movement over all history, up to the anchor."""

    history_span_months: int | None
    """``(last_year - first_year) * 12 + (last_month - first_month) + 1``; None without a valid movement."""

    months_since_last_movement: int | None
    """0 when the latest valid movement is in the anchor month."""

    material_age_months: int | None
    """Months from the first valid movement to the anchor, both inclusive; None without one."""


@dataclass(frozen=True)
class MaterialPlantInputs:
    """Everything the evaluator needs about one material-plant. Attributes come from the feature
    store; ``metrics`` is computed from staged consumption."""

    material: str
    plant: str
    mrp_type: str | None
    deletion_flag: bool | None
    """True means flagged for deletion. None (not maintained) is not deletion, as in the feature build."""

    criticality: str | None
    lead_time_days: Decimal | float | int | None

    metrics: ConsumptionMetrics

    current_stock: Decimal | None = None
    unit_price: Decimal | None = None
    group_changed_on: date | None = None
    """The day the pipeline saw this material-plant change between Min-Max and OAR, or None if it never has."""
    as_of: date | None = None
    """Today, for the cooldown. Left out (None), no cooldown applies."""
    price_currency: str | None = None
    """The currency SAP stated for ``unit_price``. A material that HAS a price is in scope only when this is
    ZAR or NAD (the currencies the OAR and Stock item money figures count); a material with no price is kept."""
    minbe: Decimal | None = None
    mabst: Decimal | None = None


@dataclass(frozen=True)
class ReverseDecision:
    """The evaluator's verdict for one material-plant."""

    category: ReverseCategory
    reason: ReverseReason
    detail: str
    """One plain sentence a planner can read; states the figures that decided it."""

    rule: int
    """The decision-table row that decided it (1-13), for audit and tests."""

    confidence: ReverseConfidence | None = None
    """High, Medium or Low for a Convert to OAR row; None for every other category."""

    points: tuple[tuple[str, str], ...] = ()
    """The reason as three short labelled lines a person can scan -- ``What we saw``, ``What it means``,
    ``What to do`` -- as (label, text) pairs. ``detail`` is the same reason as one paragraph (kept for the CSV)."""


@dataclass(frozen=True)
class AnchorResolution:
    """How the calculation anchor was fixed."""

    month: date
    """First day of the anchor month."""

    source: str
    """``"configured"`` or ``"data_load_timestamp"``."""

    latest_staged_month: date | None
    data_loaded_at: datetime | None
    warnings: tuple[str, ...] = ()

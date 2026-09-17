"""Inventory calculation value objects and statuses.

Phase 5 converts a demand forecast into stocking parameters. The models predict
demand; these formulas produce safety stock, reorder point and maximum, and they
stay deterministic and transparent so an approver can follow the arithmetic.

**Statuses are per output, not per calculation.** Lead time can succeed while
safety stock is blocked on an unsigned service level, and that is an ordinary
state -- 100% of the current catalogue is in it. One status per result would
force a choice between reporting the success or the block.

**Every trace is retained.** The Recommendation Pack shows its working, so each
result carries the intermediate values that produced it. A number nobody can
reproduce is a number nobody can challenge.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import NamedTuple

FORMULA_VERSION = "i07-formula-1"
"""Identifies the formula implementations behind a stored result.

Incremented when a formula changes. Not a semantic version -- nothing here
maintains a release contract -- but a stored result must be attributable to the
arithmetic that produced it.
"""

DAYS_PER_MONTH = Decimal("30.44")
"""Formula Reference, Stage 3B. Fixed at the documented value: 30 or 31 would
shift every lead time and therefore every safety stock."""


class LeadTimeMethod(StrEnum):
    """How a lead time was obtained. Provenance is an acceptance criterion.

    ``PLANNED_FALLBACK`` is now the only method
    :func:`app.initiatives.i7.inventory.lead_time.analyse` produces: lead time
    comes from MARC-PLIFZ unconditionally, by business decision, not from PO
    history. ``ACTUAL_STATISTICAL`` and ``ACTUAL_WARNING`` are kept on this
    enum for schema/data compatibility with anything already stored under them,
    but nothing currently emits either -- see the module docstring in
    ``inventory/lead_time.py`` for why.
    """

    ACTUAL_STATISTICAL = "ACTUAL_STATISTICAL"
    """Formerly: >= 5 valid POs, full statistical analysis. Not produced today."""

    ACTUAL_WARNING = "ACTUAL_WARNING"
    """Formerly: 2-4 POs, usable but thin. Not produced today."""

    PLANNED_FALLBACK = "PLANNED_FALLBACK"
    """SAP's planned delivery time (MARC-PLIFZ), sigma_LT = 0.3 x planned.
    Produced unconditionally, regardless of PO history."""


class CalculationStatus(StrEnum):
    """Outcome of one calculation step.

    Blocked states name *what* is missing, because the remedies differ: an
    unsigned service level is a Vedanta decision, absent lead time is a data
    gap, and an unconfigured Max Stock strategy is a design sign-off.
    """

    SUCCESS = "SUCCESS"

    WARNING = "WARNING"
    """Computed, but from a thin sample (2-4 POs)."""

    LIMITED = "LIMITED"
    """Computed from a fallback rather than observation."""

    NOT_EVALUABLE_NO_HISTORY = "NOT_EVALUABLE_NO_HISTORY"
    NOT_EVALUABLE_LEAD_TIME = "NOT_EVALUABLE_LEAD_TIME"
    NOT_EVALUABLE_SERVICE_LEVEL_UNSET = "NOT_EVALUABLE_SERVICE_LEVEL_UNSET"
    NOT_EVALUABLE_INVALID_FORECAST = "NOT_EVALUABLE_INVALID_FORECAST"
    NOT_EVALUABLE_INSUFFICIENT_DEMAND = "NOT_EVALUABLE_INSUFFICIENT_DEMAND"
    NOT_EVALUABLE_COST_DATA = "NOT_EVALUABLE_COST_DATA"

    NOT_APPLICABLE_OBSOLETE = "NOT_APPLICABLE_OBSOLETE"
    """An obsolete material gets no safety-stock recommendation. Represented
    explicitly rather than as a zero, which would read as a real target."""

    NOT_CONFIGURED = "NOT_CONFIGURED"
    """A business policy has not been signed -- the Max Stock strategy."""

    DEFERRED_TO_OAR = "DEFERRED_TO_OAR"
    """No usable history, so parameters come from the Phase 6 similarity engine.
    Not a failure: it is the documented route for cold-start materials."""

    CALCULATION_ERROR = "CALCULATION_ERROR"
    """An invariant was violated -- a negative quantity, a non-finite value.
    Surfaced rather than clamped, because clamping hides the cause."""


class LeadTimeResult(NamedTuple):
    """Lead-time analysis for one material-plant, with its full audit trail."""

    status: CalculationStatus
    method: LeadTimeMethod | None = None

    po_count: int = 0
    valid_po_count: int = 0
    excluded_cancelled_count: int = 0
    excluded_lt_error_count: int = 0
    """PO lines dropped for an implausible duration (< 1 day)."""

    outlier_count: int = 0
    """Durations beyond 730 days. Flagged and excluded from the statistics."""

    lt_avg_days: Decimal | None = None
    lt_avg_months: Decimal | None = None
    sigma_lt_days: Decimal | None = None
    sigma_lt_months: Decimal | None = None
    planned_lt_days: int | None = None
    detail: str | None = None

    @property
    def is_available(self) -> bool:
        return self.lt_avg_months is not None and self.lt_avg_months > 0


class DemandVariabilityResult(NamedTuple):
    """D_avg and sigma_D over ALL periods, zeros included.

    Distinct from the non-zero statistics CV-squared uses. The Formula Reference
    is explicit: "Include zeros -- they represent real zero-demand months."
    """

    status: CalculationStatus
    n_periods: int = 0
    zero_period_count: int = 0
    d_avg: Decimal | None = None
    sigma_d: Decimal | None = None
    detail: str | None = None


class ServiceLevelResult(NamedTuple):
    """Service level and its Z factor.

    Both ``None`` until Vedanta signs the Criticality x Circuit matrix. Z is
    derived at use rather than stored, so it can never drift from its
    percentage.
    """

    status: CalculationStatus
    service_level: Decimal | None = None
    z_factor: Decimal | None = None
    criticality: str | None = None
    circuit: str | None = None
    detail: str | None = None


class SafetyStockResult(NamedTuple):
    """Safety stock, plus everything needed to reproduce it."""

    status: CalculationStatus
    method: str | None = None
    """``normal`` (Path A), ``compound_poisson`` (Path B), ``monte_carlo``."""

    raw_safety_stock: Decimal | None = None
    safety_stock: int | None = None
    """Rounded UP to a whole unit, per the Formula Reference."""

    trace: tuple[tuple[str, str], ...] = ()
    """Ordered intermediate values -- term_1, lambda, variance_ltd. Pairs rather
    than a dict: presentation order is part of the explanation, and JSONB is off
    limits for portability."""

    detail: str | None = None


class RopResult(NamedTuple):
    """Reorder point: expected lead-time demand plus safety stock."""

    status: CalculationStatus
    expected_lead_time_demand: Decimal | None = None
    raw_rop: Decimal | None = None
    rop: int | None = None
    trace: tuple[tuple[str, str], ...] = ()
    detail: str | None = None


class MaxStockResult(NamedTuple):
    """Maximum stock. Unconfigured until Vedanta chooses a strategy."""

    status: CalculationStatus
    strategy: str | None = None
    raw_max_stock: Decimal | None = None
    max_stock: int | None = None
    trace: tuple[tuple[str, str], ...] = ()
    detail: str | None = None


class InventoryCalculationResult(NamedTuple):
    """Everything Phase 5 produces for one material-plant."""

    sap_material_number: str
    sap_plant_code: str
    demand_class: str
    selected_model: str | None
    forecast_rate: Decimal | None
    forecast_unit: str | None

    lead_time: LeadTimeResult
    variability: DemandVariabilityResult
    service_level: ServiceLevelResult
    safety_stock: SafetyStockResult
    rop: RopResult
    max_stock: MaxStockResult

    policy_id: str | None
    policy_version: int | None
    formula_version: str
    generated_at: datetime

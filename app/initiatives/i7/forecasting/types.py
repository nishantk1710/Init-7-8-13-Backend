"""Forecasting value objects and status vocabulary.

Models predict **demand only** -- a rate in units per month. Nothing here
converts a forecast into safety stock, a reorder point or a maximum, and no
model output is ever a stocking parameter. That separation is what keeps the
Phase 5 arithmetic transparent and auditable.

**Three independent statuses, deliberately not one.** They answer different
questions and can disagree:

    model_status        did the fit run?                SUCCESS / ...
    backtest_status     how much evidence do we have?   PARTIAL_DEVELOPMENT_DATA / ...
    adoption_status     may this go to production?      NOT_ELIGIBLE_... / ...

A model can fit perfectly, be backtested over 8 origins, and still be ineligible
for production because the documents require 12. Collapsing these into one field
would force that case to be reported as either a failure or an adoption, and it
is neither.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import NamedTuple


class ModelName(StrEnum):
    """The forecasting models the Solution Design routes to."""

    SES = "SES"
    AUTO_ARIMA = "AUTO_ARIMA"
    SBA = "SBA"
    LIGHTGBM = "LIGHTGBM"
    TSB = "TSB"


MODEL_VERSIONS: dict[ModelName, str] = {
    ModelName.SES: "ses-1",
    ModelName.AUTO_ARIMA: "arima-statsmodels-1",
    ModelName.SBA: "sba-1",
    ModelName.LIGHTGBM: "lgbm-quantile-1",
    ModelName.TSB: "tsb-1",
}
"""Implementation identifiers, incremented when a model's behaviour changes.

Not semantic versions: nothing here maintains a major/minor contract, and a
fabricated ``v1.3`` would imply a release history that does not exist. The
Recommendation Pack's ``SBA v1.0`` is illustrative, not a spec.
"""


class ModelStatus(StrEnum):
    """Did the model produce a forecast?"""

    SUCCESS = "SUCCESS"

    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    """Fewer observations than the model needs to fit."""

    NO_NON_ZERO_DEMAND = "NO_NON_ZERO_DEMAND"
    """Croston-family methods need at least one demand event."""

    INSUFFICIENT_NON_ZERO_OBSERVATIONS = "INSUFFICIENT_NON_ZERO_OBSERVATIONS"
    """SBA needs two events to estimate an inter-arrival interval."""

    MODEL_FIT_FAILURE = "MODEL_FIT_FAILURE"
    """The fit raised. Recorded rather than silently falling back."""

    NOT_EVALUABLE_SERVICE_LEVEL_UNSET = "NOT_EVALUABLE_SERVICE_LEVEL_UNSET"
    """LightGBM is a quantile model and the target quantile comes from the
    service-level matrix, which Vedanta has not signed. Guessing a quantile
    would produce a plausible, unattributable forecast."""

    NOT_EVALUABLE_TRIGGER_UNSET = "NOT_EVALUABLE_TRIGGER_UNSET"
    """TSB applies when an approved obsolescence rule flags a material. No such
    rule is configured, and MSTAE='01' is not a substitute for one."""

    NOT_EVALUABLE_UNIT_UNKNOWN = "NOT_EVALUABLE_UNIT_UNKNOWN"
    """No unit of measure, so the forecast rate has no meaning."""

    MISSING_REQUIRED_FEATURE = "MISSING_REQUIRED_FEATURE"


class BacktestStatus(StrEnum):
    """How much rolling-origin evidence exists."""

    COMPLETE = "COMPLETE"
    """At least the required origins (12) were evaluated."""

    PARTIAL_DEVELOPMENT_DATA = "PARTIAL_DEVELOPMENT_DATA"
    """Some origins, but fewer than required. The expected state on the
    July/August extract: a 13-month window yields at most 10 origins."""

    NOT_EVALUABLE_INSUFFICIENT_HISTORY = "NOT_EVALUABLE_INSUFFICIENT_HISTORY"
    NOT_EVALUABLE_LEAD_TIME_UNAVAILABLE = "NOT_EVALUABLE_LEAD_TIME_UNAVAILABLE"
    """No lead time, so the T+1..T+LT horizon is undefined. Never replaced with
    a guessed 30/60/90 days -- the lead-time policy is unresolved."""

    NOT_EVALUABLE = "NOT_EVALUABLE"


class AdoptionStatus(StrEnum):
    """May a challenger replace its baseline in production?"""

    BASELINE_RETAINED = "BASELINE_RETAINED"
    """The challenger did not clear the documented bars."""

    CHALLENGER_ELIGIBLE = "CHALLENGER_ELIGIBLE"
    """Improvement criteria met AND sufficient origins. The decision itself
    still belongs to a human."""

    NOT_ELIGIBLE_INSUFFICIENT_ORIGINS = "NOT_ELIGIBLE_INSUFFICIENT_ORIGINS"
    """The metrics may look better, but the evidence requirement is unmet. This
    is the honest answer on the current extract -- not "adopted", not "failed"."""

    NOT_EVALUABLE = "NOT_EVALUABLE"
    """The challenger could not run at all."""


class MetricStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    NOT_EVALUABLE = "NOT_EVALUABLE"
    """Inputs the metric needs do not exist -- holding cost without a holding
    rate, pinball loss without a target quantile."""


class DemandPoint(NamedTuple):
    """One month of a prepared demand series."""

    period: date
    quantity: Decimal


class ForecastResult(NamedTuple):
    """One model's forecast for one material-plant.

    ``rate`` is units per month and is ``None`` whenever ``status`` is not
    SUCCESS -- an unavailable forecast is absent, never zero. A zero rate is a
    real prediction (this material will not move), and conflating it with "could
    not forecast" would push a confident zero into Phase 5.
    """

    model: ModelName
    model_version: str
    status: ModelStatus
    rate: Decimal | None = None
    unit: str | None = None
    training_start: date | None = None
    training_end: date | None = None
    horizon_months: int | None = None
    parameters: tuple[tuple[str, str], ...] = ()
    """Model-specific metadata: SES alpha, ARIMA (p,d,q), SBA alpha/p/z, TSB
    alpha/beta. Ordered pairs rather than a dict -- these are persisted, and
    JSONB is off-limits for portability. Only values actually computed appear.
    """

    detail: str | None = None

    @property
    def is_available(self) -> bool:
        return self.status is ModelStatus.SUCCESS and self.rate is not None


class OriginForecast(NamedTuple):
    """One forecast/actual pair at one rolling origin and horizon step.

    The complete path is retained rather than collapsed to a single number: the
    Phase 5 calculations and the audit trail both need to see how a model behaved
    step by step, not just its mean error.
    """

    origin_period: date
    horizon_step: int
    forecast_period: date
    predicted: Decimal
    actual: Decimal


class BacktestMetrics(NamedTuple):
    """Evaluation of one model over its rolling origins."""

    pinball_loss: Decimal | None
    pinball_status: MetricStatus
    mean_error: Decimal | None
    bias_percentage: Decimal | None
    fill_rate: Decimal | None
    fill_rate_status: MetricStatus
    holding_cost: Decimal | None
    holding_cost_status: MetricStatus
    mean_absolute_error: Decimal | None


class BacktestResult(NamedTuple):
    """A model's rolling-origin evidence for one material-plant."""

    model: ModelName
    model_version: str
    status: BacktestStatus
    required_origins: int
    available_origins: int
    origins_evaluated: int
    metrics: BacktestMetrics | None = None
    paths: tuple[OriginForecast, ...] = ()
    detail: str | None = None


class SegmentDecision(NamedTuple):
    """A champion/challenger comparison, one per material-plant.

    Named for the table it is stored in (``segment_key`` historically held a
    demand-class name); the FRS (Section 3.1, FR-3) requires model selection
    per material by backtest, so ``segment_key`` now holds
    ``"{material}/{plant}"`` and this is computed once per material-plant, not
    pooled across a demand class.
    """

    segment_key: str
    baseline_model: ModelName
    challenger_model: ModelName | None
    baseline_pinball: Decimal | None
    challenger_pinball: Decimal | None
    improvement: Decimal | None
    """Fractional improvement in pinball loss, positive meaning better."""

    baseline_bias: Decimal | None
    challenger_bias: Decimal | None
    bias_change: Decimal | None
    origins_available: int
    origins_evaluated: int
    required_origins: int
    adoption_status: AdoptionStatus
    decision_reason: str
    generated_at: datetime

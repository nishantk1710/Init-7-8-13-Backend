"""The recommendation contract.

Shape only. Nothing here computes a safety stock -- later phases populate these
fields; Phase 1 establishes what a recommendation *is*.

**Unavailable is None, never zero.** The frontend's SAP mapper currently emits
``serviceLevelTarget: 0``, ``unitPrice: 0`` and ``leadTimeVarianceDays: 0`` for
values it cannot source, and the consequences are instructive: the UI renders a
0% service level as though it were a target, and ``Phi^-1(0)`` is negative
infinity. A zero that means "unknown" is indistinguishable from a zero that
means zero, and only one of them is safe to calculate with. So every value that
may be unavailable is ``| None``, and the API layer decides how to present
absence.

**Provenance travels with the numbers.** Policy version, model choice, lead-time
source and confidence are part of the recommendation, not metadata alongside it.
Every acceptance criterion in the FRS about traceability is satisfied by the
recommendation alone.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.initiatives.i7.contracts.enums import (
    ConfidenceGrade,
    Criticality,
    DataQualityGrade,
    DemandPattern,
    LeadTimeSource,
    RecommendationStatus,
    RiskLevel,
)
from app.initiatives.i7.contracts.identity import MaterialPlantKey, PolicyVersionRef


class StockParameters(BaseModel):
    """A safety stock / reorder point / maximum triple.

    Each is independently nullable: SAP may hold a reorder point and no maximum,
    and the recommended maximum is unavailable while the Max Stock strategy is
    unchosen. A partially-known set is the normal case.
    """

    model_config = ConfigDict(frozen=True)

    safety_stock: Decimal | None = None
    reorder_point: Decimal | None = None
    maximum_stock: Decimal | None = None


class ConsumptionPoint(BaseModel):
    """One point of the history shown on a recommendation."""

    model_config = ConfigDict(frozen=True)

    period: date
    quantity: Decimal


class RecommendationFactor(BaseModel):
    """One human-readable driver behind the recommendation."""

    model_config = ConfigDict(frozen=True)

    label: str = Field(min_length=1)
    detail: str = Field(min_length=1)


class ModelProfile(BaseModel):
    """A forecasting model and how it scored."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    version: str | None = None
    description: str | None = None
    pinball_loss: float | None = None
    bias: float | None = None


class ChampionChallenger(BaseModel):
    """Which model won, which lost, and why.

    A challenger is adopted only after backtesting clears the configured bars,
    so ``selected`` records an outcome rather than a preference.
    """

    model_config = ConfigDict(frozen=True)

    champion: ModelProfile
    challenger: ModelProfile | None = None
    selected: str = Field(min_length=1)
    """``"champion"`` or ``"challenger"``."""

    backtest_origins: int | None = None
    rationale: str | None = None


class LeadTimeProfile(BaseModel):
    """Lead time, its spread, and where it came from.

    ``source`` is mandatory: every recommendation's lead time must be traceable
    to the I11 program or explicitly flagged as a planned-delivery-time
    placeholder.
    """

    model_config = ConfigDict(frozen=True)

    source: LeadTimeSource
    mean_days: float | None = None
    standard_deviation_days: float | None = None
    """``None`` when there is too little PO history to measure spread. Not 0 --
    zero variance is a strong claim about a supplier."""

    observation_count: int | None = Field(default=None, ge=0)


class OarColdStartGuidance(BaseModel):
    """Parameters borrowed from similar materials, for a material with no history."""

    model_config = ConfigDict(frozen=True)

    neighbour_keys: tuple[str, ...] = ()
    neighbour_count: int | None = Field(default=None, ge=0)
    best_similarity: float | None = Field(default=None, ge=0, le=1)
    suggested: StockParameters = Field(default_factory=StockParameters)
    note: str | None = None


class WorkflowStep(BaseModel):
    """One step of the approval chain.

    ``decided_at`` is a real timestamp. The frontend currently reverse-engineers
    approval times by regex from display strings like "Approved 6d ago"; the
    backend supplies the actual value so that stops being necessary.
    """

    model_config = ConfigDict(frozen=True)

    step_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    status: str = Field(min_length=1)
    actor: str | None = None
    decided_at: datetime | None = None
    comment: str | None = None


class CalculationTrace(BaseModel):
    """Intermediate values behind the numbers.

    The Recommendation Pack shows its working -- lambda, E[d^2], Var[LTD] -- so
    an approver can follow the arithmetic. Stored as ordered label/value pairs
    rather than a dict: presentation order is part of the explanation, and
    ``JSONB`` is off-limits for portability.
    """

    model_config = ConfigDict(frozen=True)

    entries: tuple[tuple[str, str], ...] = ()


class Recommendation(BaseModel):
    """A proposed change to one material-plant's stocking parameters."""

    model_config = ConfigDict(frozen=True)

    recommendation_id: str = Field(min_length=1)
    key: MaterialPlantKey

    # --- Classification and context ------------------------------------
    criticality: Criticality | None = None
    """Material attribute -- deliberately not the same axis as ``risk``."""

    circuit: str | None = None
    demand_pattern: DemandPattern = DemandPattern.UNCLASSIFIED
    risk: RiskLevel | None = None
    """Recommendation attribute -- how exposed this material is now."""

    is_oar: bool | None = None
    """``None`` where scope evaluated to UNKNOWN."""

    # --- The proposal ---------------------------------------------------
    status: RecommendationStatus = RecommendationStatus.PENDING_REVIEW
    current: StockParameters = Field(default_factory=StockParameters)
    recommended: StockParameters = Field(default_factory=StockParameters)

    # --- Inputs, each nullable when unavailable --------------------------
    average_monthly_demand: Decimal | None = None
    demand_standard_deviation: Decimal | None = None
    annual_consumption: Decimal | None = None
    lead_time: LeadTimeProfile | None = None
    service_level_target: float | None = Field(default=None, gt=0, lt=1)
    """A fraction (0.98 = 98%). ``None`` while the matrix is unsigned -- never 0,
    which would put ``Phi^-1(0)`` into the safety-stock formula."""

    unit_price: Decimal | None = None
    currency: str | None = None
    working_capital_impact: Decimal | None = None
    """Positive releases capital, negative ties it up -- the frontend's
    convention, kept so the sign does not flip crossing the API."""

    # --- Explainability --------------------------------------------------
    consumption_history: tuple[ConsumptionPoint, ...] = ()
    factors: tuple[RecommendationFactor, ...] = ()
    champion_challenger: ChampionChallenger | None = None
    calculation_trace: CalculationTrace | None = None
    oar_cold_start: OarColdStartGuidance | None = None

    # --- Grading ----------------------------------------------------------
    confidence: ConfidenceGrade | None = None
    data_quality: DataQualityGrade | None = None
    history_months: int | None = Field(default=None, ge=0)

    # --- Workflow and provenance -------------------------------------------
    workflow: tuple[WorkflowStep, ...] = ()
    policy: PolicyVersionRef
    """Mandatory. A recommendation that cannot name the policy that produced it
    is not auditable, so there is no default."""

    generated_at: datetime

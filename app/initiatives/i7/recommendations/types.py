"""Phase 7 value objects: lifecycle status, conversion, workflow, adoption.

Deliberately separate from :class:`~app.initiatives.i7.contracts.enums.RecommendationStatus`.
That enum mirrors the frontend's six-value UI vocabulary (`"Pending Review"`,
`"In Approval"`, ...); it has no way to say "the service-level matrix is
unsigned" or "final approval reached, awaiting manual SAP execution" without
inventing a UI-facing meaning for a purely internal state. ``LifecycleStatus``
is the backend's own state machine; :func:`to_recommendation_status` is the one
place that projects it down to the UI vocabulary Phase 8's API will expose.

A recommendation being blocked is not an error -- it is the majority state of
this pipeline today, because the service-level matrix is unsigned. Every status
below is chosen to make *why* visible rather than collapsing every non-ready
state into one generic "blocked".
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import NamedTuple

from app.initiatives.i7.contracts.enums import RecommendationStatus

RECOMMENDATION_FORMULA_VERSION = "i07-recommendation-2"
"""Bumped from ``-1``: the OAR builder previously granted READY_FOR_REVIEW on
similarity evidence alone, before the weighted estimate was actually
available. Recommendations produced under the old logic must not be mistaken
for ones produced under the corrected rule -- the version is part of the
idempotency key precisely so a logic fix forces regeneration rather than being
silently masked by an unchanged-inputs reuse."""
"""Identifies this phase's assembly logic, distinct from Phase 4/5's own
``FORMULA_VERSION`` constants -- a recommendation can change shape (a new
explanation rule, a new workflow) without any upstream calculation changing."""


class LifecycleStatus(StrEnum):
    """The backend's own recommendation state machine."""

    NOT_EVALUABLE = "NOT_EVALUABLE"
    """A required upstream input is missing or blocked. Never promoted to
    READY_FOR_REVIEW -- an approver must not be asked to approve a number that
    was never actually computed."""

    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    """Every mandatory input for this recommendation's own path was
    computable. Not yet submitted into the approval chain."""

    PENDING_APPROVAL = "PENDING_APPROVAL"
    """Submitted; awaiting one specific role's decision."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SENT_BACK = "SENT_BACK"
    ADJUSTED = "ADJUSTED"
    """A reviewer changed a value before continuing the chain. Never a silent
    edit -- see :mod:`workflow`."""

    SAP_EXECUTION_PENDING = "SAP_EXECUTION_PENDING"
    """Final approval reached. I07 does not call SAP; VZI executes manually."""

    SAP_EXECUTED = "SAP_EXECUTED"
    """Execution evidence recorded. Not proof of adoption -- see
    :class:`AdoptionStatus`, which reads SAP state independently."""

    ADOPTED = "ADOPTED"
    PARTIALLY_ADOPTED = "PARTIALLY_ADOPTED"
    NOT_ADOPTED = "NOT_ADOPTED"


_TO_RECOMMENDATION_STATUS: dict[LifecycleStatus, RecommendationStatus] = {
    LifecycleStatus.NOT_EVALUABLE: RecommendationStatus.PENDING_REVIEW,
    LifecycleStatus.READY_FOR_REVIEW: RecommendationStatus.PENDING_REVIEW,
    LifecycleStatus.PENDING_APPROVAL: RecommendationStatus.IN_APPROVAL,
    LifecycleStatus.APPROVED: RecommendationStatus.APPROVED,
    LifecycleStatus.REJECTED: RecommendationStatus.REJECTED,
    LifecycleStatus.SENT_BACK: RecommendationStatus.RETURNED,
    LifecycleStatus.ADJUSTED: RecommendationStatus.IN_APPROVAL,
    LifecycleStatus.SAP_EXECUTION_PENDING: RecommendationStatus.APPROVED,
    LifecycleStatus.SAP_EXECUTED: RecommendationStatus.IMPLEMENTED,
    LifecycleStatus.ADOPTED: RecommendationStatus.IMPLEMENTED,
    LifecycleStatus.PARTIALLY_ADOPTED: RecommendationStatus.IMPLEMENTED,
    LifecycleStatus.NOT_ADOPTED: RecommendationStatus.IMPLEMENTED,
}


def to_recommendation_status(status: LifecycleStatus) -> RecommendationStatus:
    """Project the internal state machine onto the frontend's six-value enum.

    A many-to-one mapping by design: the UI does not yet distinguish
    "awaiting manual SAP execution" from "approved", and this is the seam
    Phase 8's API translates through rather than a place that invents new
    frontend states.
    """
    return _TO_RECOMMENDATION_STATUS[status]


class ApprovalRole(StrEnum):
    """The four-step chain, exactly as approved. Order is load-bearing."""

    END_USER = "End User"
    ENGINEERING_MANAGER = "Engineering Manager"
    COMMERCIAL_MANAGER = "Commercial Manager"
    WAREHOUSE_SUPERVISOR = "Warehouse Supervisor"


APPROVAL_CHAIN: tuple[ApprovalRole, ...] = (
    ApprovalRole.END_USER,
    ApprovalRole.ENGINEERING_MANAGER,
    ApprovalRole.COMMERCIAL_MANAGER,
    ApprovalRole.WAREHOUSE_SUPERVISOR,
)


class ApprovalAction(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    SEND_BACK = "SEND_BACK"
    ADJUST = "ADJUST"


class ConversionTrigger(StrEnum):
    """Which of the three OR-ed conditions produced the eligibility verdict."""

    CONSUMPTION_FREQUENCY = "CONSUMPTION_FREQUENCY"
    PRODUCTION_IMPACT = "PRODUCTION_IMPACT"
    I13_HOD_APPROVED_REQUEST = "I13_HOD_APPROVED_REQUEST"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"
    """At least one enabled trigger could not be evaluated (its policy input is
    unresolved) and none of the others fired. Distinct from NONE -- NONE means
    every trigger was checked and definitively did not fire."""


class ConversionEligibility(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    UNKNOWN = "UNKNOWN"


class ConversionDecision(NamedTuple):
    """OAR -> Min-Max conversion eligibility for one material-plant.

    Identification and conversion are separate decisions (Solution Design,
    Stage 5A) -- a material can be OAR without ever reaching this evaluation,
    and this evaluation never reclassifies OAR status.
    """

    eligibility: ConversionEligibility
    trigger: ConversionTrigger
    consumption_count_12m: int | None
    consumption_count_threshold: int | None
    production_impact: bool | None
    i13_hod_approved: bool | None
    detail: str


class ImpactStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    NOT_EVALUABLE_MISSING_CURRENT = "NOT_EVALUABLE_MISSING_CURRENT"
    NOT_EVALUABLE_MISSING_RECOMMENDED = "NOT_EVALUABLE_MISSING_RECOMMENDED"
    NOT_EVALUABLE_COST_DATA_UNAVAILABLE = "NOT_EVALUABLE_COST_DATA_UNAVAILABLE"


class ParameterDelta(NamedTuple):
    """Current vs recommended for one stocking parameter."""

    current: Decimal | None
    recommended: Decimal | None
    delta: Decimal | None
    percent_change: Decimal | None
    """``None`` when ``current`` is zero or unknown -- a percentage of nothing
    has no meaning, and 0% would read as "no change"."""


class ExpectedImpact(NamedTuple):
    """Conservative current-vs-recommended comparison. No monetary figure
    unless cost data genuinely exists."""

    status: ImpactStatus
    safety_stock: ParameterDelta | None = None
    reorder_point: ParameterDelta | None = None
    maximum_stock: ParameterDelta | None = None
    monetary_impact: Decimal | None = None
    detail: str | None = None


class AdoptionStatus(StrEnum):
    ADOPTED = "ADOPTED"
    PARTIALLY_ADOPTED = "PARTIALLY_ADOPTED"
    NOT_ADOPTED = "NOT_ADOPTED"
    UNKNOWN = "UNKNOWN"
    """No SAP evidence exists to compare against. Never conflated with
    NOT_ADOPTED, which asserts a proven unchanged state."""


class AdoptionResult(NamedTuple):
    status: AdoptionStatus
    expected: tuple[tuple[str, str], ...]
    observed: tuple[tuple[str, str], ...]
    matched_fields: tuple[str, ...]
    mismatched_fields: tuple[str, ...]
    detail: str


class ExecutionStatus(StrEnum):
    PENDING = "PENDING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    NOT_CONFIRMED = "NOT_CONFIRMED"


class ApprovalEvent(NamedTuple):
    """One immutable ledger entry."""

    recommendation_id: str
    recommendation_version: int
    actor_id: str
    actor_role: ApprovalRole
    action: ApprovalAction
    previous_status: LifecycleStatus
    new_status: LifecycleStatus
    comment: str | None
    timestamp: datetime


class WorkflowError(Exception):
    """An approval action was rejected by the state machine.

    Distinct from the domain errors in :mod:`app.initiatives.i7.errors`: this
    is a workflow-rule violation (wrong role, missing comment, skipped stage),
    not a data or configuration problem.
    """

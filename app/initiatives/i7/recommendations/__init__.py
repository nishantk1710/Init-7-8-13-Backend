"""I07 recommendation, approval workflow, and SAP adoption evidence.

    Phase 3/4/5/6 outputs
        -> builder (assembles, never recalculates)
        -> explanation (deterministic reasons + trace, no LLM)
        -> conversion (OAR -> Min-Max eligibility)
        -> workflow (four-step approval state machine)
        -> ledger (append-only audit trail)
        -> execution (manual SAP evidence capture, never a SAP call)
        -> adoption (read-only reconciliation)

**AI recommends. Humans approve. SAP is updated manually.** Nothing in this
package can write to SAP -- there is no HTTP client, no OData call, and no
adapter that reaches outward. ``execution.py`` records what a human reports;
``adoption.py`` reads evidence through an interface that has no implementation
today, so it reports UNKNOWN rather than querying a raw table.

| Module | Responsibility |
| --- | --- |
| ``repository`` | bulk reads of the latest Phase 3/4/5/6 runs |
| ``builder`` | assembles one recommendation per material-plant |
| ``explanation`` | deterministic reasons, trace, expected impact |
| ``conversion`` | OAR -> Min-Max trigger evaluation |
| ``workflow`` | the four-step approval state machine (pure) |
| ``ledger`` | applies workflow actions, writes the immutable audit trail |
| ``execution`` | manual SAP execution evidence capture |
| ``adoption`` | read-only SAP state reconciliation |
| ``service`` | batch recommendation generation |
"""

from app.initiatives.i7.recommendations.service import (
    RecommendationRunResult,
    generate_recommendations,
)
from app.initiatives.i7.recommendations.types import (
    RECOMMENDATION_FORMULA_VERSION,
    AdoptionResult,
    AdoptionStatus,
    ApprovalAction,
    ApprovalRole,
    ConversionDecision,
    ConversionEligibility,
    ConversionTrigger,
    ExecutionStatus,
    ExpectedImpact,
    LifecycleStatus,
    WorkflowError,
)

__all__ = [
    "RECOMMENDATION_FORMULA_VERSION",
    "AdoptionResult",
    "AdoptionStatus",
    "ApprovalAction",
    "ApprovalRole",
    "ConversionDecision",
    "ConversionEligibility",
    "ConversionTrigger",
    "ExecutionStatus",
    "ExpectedImpact",
    "LifecycleStatus",
    "RecommendationRunResult",
    "WorkflowError",
    "generate_recommendations",
]

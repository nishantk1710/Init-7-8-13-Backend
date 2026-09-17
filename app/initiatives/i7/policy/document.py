"""The versioned policy document -- every I07 business rule in one object.

A recommendation must be explainable months later, to an approver asking why a
safety stock of 19 was proposed. That needs the *exact* rules used, not today's
rules: thresholds get calibrated and service levels get re-signed, and a
recommendation re-derived under new policy would not reproduce.

So the document is immutable and identified by ``policy_id`` plus
``policy_version``. A recommendation stores that pair, and the pair is enough to
reconstruct the arithmetic.

``validate()`` is deliberately separate from construction. Building a document
with unresolved policies must stay legal -- that is the state the project is
actually in -- while *calculating* with one must not. Construction answers "is
this well-formed?"; :meth:`PolicyDocument.require_ready_for_recommendations`
answers "may this produce recommendations?", and they have different answers
right now.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.initiatives.i7.contracts.enums import PolicyStatus
from app.initiatives.i7.contracts.identity import PolicyVersionRef
from app.initiatives.i7.errors import ConfigurationError, PolicyNotConfiguredError
from app.initiatives.i7.policy.oar import OarPolicy, current_oar_policy
from app.initiatives.i7.policy.thresholds import (
    AdoptionPolicy,
    ClassificationPolicy,
    ConfidencePolicy,
    ConversionTriggerPolicy,
    HistoryGatePolicy,
    LeadTimePolicy,
    ModelAdoptionPolicy,
    SimilarityPolicy,
)
from app.initiatives.i7.policy.unresolved import MaxStockStrategy, ServiceLevelPolicy


class PolicyDocument(BaseModel):
    """One immutable version of the complete I07 business policy."""

    model_config = ConfigDict(frozen=True)

    policy_id: str = Field(default="i07-default", min_length=1)
    policy_version: int = Field(default=1, ge=1)
    status: PolicyStatus = PolicyStatus.DRAFT
    """DRAFT until Vedanta signs. Only SIGNED may produce recommendations."""

    effective_from: date | None = None
    effective_to: date | None = None

    oar: OarPolicy = Field(default_factory=current_oar_policy)
    history_gate: HistoryGatePolicy = Field(default_factory=HistoryGatePolicy)
    classification: ClassificationPolicy = Field(default_factory=ClassificationPolicy)
    model_adoption: ModelAdoptionPolicy = Field(default_factory=ModelAdoptionPolicy)
    similarity: SimilarityPolicy = Field(default_factory=SimilarityPolicy)
    confidence: ConfidencePolicy = Field(default_factory=ConfidencePolicy)
    lead_time: LeadTimePolicy = Field(default_factory=LeadTimePolicy)
    conversion_triggers: ConversionTriggerPolicy = Field(default_factory=ConversionTriggerPolicy)
    adoption: AdoptionPolicy = Field(default_factory=AdoptionPolicy)

    # Unresolved. Default to empty, never to a guess.
    service_level: ServiceLevelPolicy = Field(default_factory=ServiceLevelPolicy)
    max_stock: MaxStockStrategy = Field(default_factory=MaxStockStrategy)

    @property
    def reference(self) -> PolicyVersionRef:
        return PolicyVersionRef(policy_id=self.policy_id, policy_version=self.policy_version)

    def validate_document(self) -> None:
        """Check cross-policy consistency. Raises :class:`ConfigurationError`.

        Within-policy rules (weights summing to 1, ordered confidence bands) are
        enforced by each model's own validators, so this only covers what spans
        policies or the document itself.
        """
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ConfigurationError(
                f"effective_to ({self.effective_to}) precedes effective_from ({self.effective_from})"
            )

        if self.confidence.high_minimum_history_months < self.history_gate.minimum_history_months:
            raise ConfigurationError(
                "confidence HIGH requires less history than the history gate demands, "
                "so a material could be graded HIGH without ever being classified"
            )

        if (
            self.similarity.minimum_neighbour_history_months
            < self.history_gate.minimum_history_months
        ):
            raise ConfigurationError(
                "OAR similarity would accept neighbours with too little history to "
                "have been classified themselves"
            )

    def unresolved_policies(self) -> tuple[str, ...]:
        """Names of policies still awaiting a business decision.

        Drives an operator-facing readiness report: what is Vedanta blocked on.
        """
        unresolved: list[str] = []
        if not self.service_level.is_configured:
            unresolved.append("service_level_matrix")
        if not self.max_stock.is_configured:
            unresolved.append("max_stock_strategy")
        if self.adoption.monitoring_window_days is None:
            unresolved.append("adoption_monitoring_window")
        if (
            self.conversion_triggers.enable_criticality_trigger
            and self.conversion_triggers.criticality_trigger_tiers is None
        ):
            unresolved.append("conversion_criticality_tiers")
        if not self.oar.confirmed:
            unresolved.append("oar_rule_confirmation")
        if self.oar.rollup is None:
            unresolved.append("oar_rollup_policy")
        return tuple(unresolved)

    @property
    def is_ready_for_recommendations(self) -> bool:
        return self.status is PolicyStatus.SIGNED and not self.unresolved_policies()

    def require_ready_for_recommendations(self) -> None:
        """Gate before any recommendation is produced.

        Enforces the Solution Design's rule that an unsigned policy blocks
        recommendations.
        """
        if self.status is not PolicyStatus.SIGNED:
            raise PolicyNotConfiguredError(
                "policy_status",
                f"Policy {self.reference} is {self.status}, not SIGNED. "
                "Recommendations are blocked until the policy is signed off.",
            )
        outstanding = self.unresolved_policies()
        if outstanding:
            raise PolicyNotConfiguredError(
                "policy_document",
                f"Policy {self.reference} has unresolved policies: {', '.join(outstanding)}.",
            )

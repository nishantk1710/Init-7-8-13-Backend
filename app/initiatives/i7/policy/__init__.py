"""I07 business policy.

Separate from ``app.core.config``, and the split is deliberate.
:class:`~app.core.config.Settings` holds *infrastructure* -- connection strings,
credentials, timeouts -- which vary by environment and are set by whoever
deploys. Policy holds *business rules* -- service levels, classification
cutoffs, the OAR definition -- which are the same in every environment and are
set by Vedanta.

They also version differently. Infrastructure is current-value-only: nobody asks
what the database URL was in March. Policy must be versioned and immutable,
because a recommendation has to be explainable against the rules in force when
it was produced.

Two states are kept distinct throughout: a *configured* value (present, valid,
usable) and an *unconfigured* one (no business decision yet). The second raises
:class:`~app.initiatives.i7.errors.PolicyNotConfiguredError` rather than falling
back to a default, because a plausible guess at a service level yields a
plausible, wrong, approved safety stock.
"""

from app.initiatives.i7.policy.document import PolicyDocument, PolicyVersionRef
from app.initiatives.i7.policy.oar import (
    OarPolicy,
    PredicateField,
    PredicateOperator,
    RollupPolicy,
    ScopePredicate,
    current_oar_policy,
)
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
from app.initiatives.i7.policy.unresolved import (
    MaxStockStrategy,
    ServiceLevelKey,
    ServiceLevelPolicy,
)

__all__ = [
    "AdoptionPolicy",
    "ClassificationPolicy",
    "ConfidencePolicy",
    "ConversionTriggerPolicy",
    "HistoryGatePolicy",
    "LeadTimePolicy",
    "MaxStockStrategy",
    "ModelAdoptionPolicy",
    "OarPolicy",
    "PolicyDocument",
    "PolicyVersionRef",
    "PredicateField",
    "PredicateOperator",
    "RollupPolicy",
    "ScopePredicate",
    "ServiceLevelKey",
    "ServiceLevelPolicy",
    "SimilarityPolicy",
    "current_oar_policy",
]

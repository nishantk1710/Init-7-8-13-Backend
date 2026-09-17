"""OAR scope evaluation at material-plant grain.

Thin by design. The rule itself lives in the Phase 1
:class:`~app.initiatives.i7.policy.OarPolicy` -- a predicate list combined by
AND -- and this module adds only what the feature store needs on top: a reason
string explaining *which* predicate decided, and the unresolved roll-up state.

No MRP type or material status value appears here. Configuration owns those, and
a leakage test fails the build if one leaks out.

Three states, and UNKNOWN is the one that matters. A blank DISMM means "not
maintained", which is not "not OAR" -- 47% of rows in the live scan had no value.
Folding those into OUT_OF_SCOPE would drop half the catalogue out of the OAR
population with nothing to show it happened.
"""

from typing import NamedTuple

from app.initiatives.i7.contracts import MaterialAttributes, ScopeDecision
from app.initiatives.i7.policy import OarPolicy
from app.initiatives.i7.policy.oar import RollupPolicy

ROLLUP_NOT_CONFIGURED = "NOT_CONFIGURED"
"""Material-level roll-up has not been decided. Recorded as unresolved rather
than defaulting to the current likely direction -- ``per-plant-only`` is the
conservative guess, but it is still a guess, and a stored guess is
indistinguishable from a ruling once it is in the data."""


class OarAssessment(NamedTuple):
    """An OAR verdict for one material-plant, with its explanation."""

    scope: ScopeDecision
    reason: str
    rollup_status: str


def assess_oar_scope(attributes: MaterialAttributes, policy: OarPolicy) -> OarAssessment:
    """Evaluate the configured OAR rule against one material-plant.

    The verdict comes from the policy; the reason is reconstructed here by
    re-evaluating each predicate individually, so an UNKNOWN can name the field
    that was missing rather than just reporting that something was.
    """
    scope = policy.evaluate(attributes)

    excluded: list[str] = []
    unknown: list[str] = []
    for predicate in policy.predicates:
        match predicate.evaluate(attributes):
            case ScopeDecision.OUT_OF_SCOPE:
                excluded.append(predicate.field.value)
            case ScopeDecision.UNKNOWN:
                unknown.append(predicate.field.value)

    match scope:
        case ScopeDecision.OUT_OF_SCOPE:
            reason = f"excluded by {', '.join(excluded)}"
        case ScopeDecision.UNKNOWN:
            reason = f"not maintained: {', '.join(unknown)}"
        case _:
            reason = "all predicates satisfied"

    rollup_status = (
        policy.rollup.value if isinstance(policy.rollup, RollupPolicy) else ROLLUP_NOT_CONFIGURED
    )

    return OarAssessment(scope, reason, rollup_status)

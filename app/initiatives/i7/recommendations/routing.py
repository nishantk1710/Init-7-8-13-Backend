"""Which approval route a recommendation follows.

Two independent routes, resolved from persisted, existing recommendation
fields -- never invented at the API layer:

* OAR conversion recommendations (``is_oar=True``) always take
  ``OAR_APPROVAL_CHAIN`` -- Inventory Controller -> Commercial Head ->
  Engineering Head -> Plant Head. Fixed, regardless of criticality.
* ROP/Max recommendations (``is_oar=False``) are routed by criticality tier,
  through ``PolicyDocument.approval_routing`` -- a configuration lookup, not
  a branch written here.

Both routes are enforced by the same ``workflow`` state machine; this module
only decides which tuple of roles that machine uses for one recommendation.
"""

from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations.types import (
    APPROVAL_CHAIN,
    OAR_APPROVAL_CHAIN,
    ApprovalRole,
)


def route_for(is_oar: bool | None, criticality: str | None, policy: PolicyDocument) -> tuple[ApprovalRole, ...]:
    """The approval route for one recommendation.

    ``is_oar`` and ``criticality`` are read from the persisted
    ``Recommendation`` row, never recomputed -- routing is a policy lookup
    over facts Phase 7 already established, not a new business rule.
    """
    if is_oar:
        return OAR_APPROVAL_CHAIN

    configured = policy.approval_routing.route_for(criticality)
    try:
        return tuple(ApprovalRole(value) for value in configured)
    except ValueError:
        # An unrecognised role value in a misconfigured policy must not
        # silently produce an empty or wrong route -- fall back to the
        # historical default rather than let a typo in configuration reach
        # the workflow machine as a route nobody can act on.
        return APPROVAL_CHAIN

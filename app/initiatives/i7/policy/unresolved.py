"""Policies Vedanta has not decided yet.

Separated from :mod:`app.initiatives.i7.policy.thresholds` because these behave
differently: a threshold has a documented value and therefore a default, while
everything here has **no** value and must never acquire one by accident.

The Solution Design is explicit about the service-level matrix:

    THIS MUST COME FROM VEDANTA, NOT FROM CODE
    Do NOT invent percentages. Do NOT assume 98% for critical.
    If policy is unsigned -> system blocks recommendations

So the accessors raise :class:`PolicyNotConfiguredError` rather than returning a
fallback. A wrong service level does not fail loudly -- it produces a safety
stock that looks entirely reasonable, cites a Z factor nobody approved, and gets
signed off. Blocking is the safe failure.

Max Stock is the same shape: the strategy is unchosen, Option A needs ordering
cost and holding rate that appear in no document and no table, and Option B
needs review periods nobody has supplied.
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.initiatives.i7.contracts.enums import Criticality
from app.initiatives.i7.errors import PolicyNotConfiguredError


class ServiceLevelKey(BaseModel):
    """What a service level is looked up by: criticality, and circuit.

    ``circuit`` is nullable because no SAP field supplies it. A matrix may be
    keyed on criticality alone, with ``circuit=None`` as the wildcard.
    """

    model_config = ConfigDict(frozen=True)

    criticality: Criticality
    circuit: str | None = None


class ServiceLevelPolicy(BaseModel):
    """Criticality x Circuit -> service level. Empty until signed.

    Z is deliberately absent: it is ``Phi^-1(service_level)``, computed where it
    is used, so a stored Z can never drift out of step with its percentage.
    """

    model_config = ConfigDict(frozen=True)

    matrix: tuple[tuple[ServiceLevelKey, float], ...] = ()
    """Empty by default. Populated only from a signed Vedanta matrix."""

    @property
    def is_configured(self) -> bool:
        return len(self.matrix) > 0

    def service_level_for(self, criticality: Criticality, circuit: str | None = None) -> float:
        """Look up a target service level as a fraction (0.98 = 98%).

        Raises :class:`PolicyNotConfiguredError` when unsigned, or when signed
        but silent on this combination -- an unlisted pair is a gap in the
        matrix, not licence to fall back to a neighbouring cell.
        """
        if not self.is_configured:
            raise PolicyNotConfiguredError(
                "service_level_matrix",
                "The Criticality x Circuit service-level matrix must be supplied "
                "and signed by Vedanta. Recommendations are blocked until then "
                "(Solution Design, Rule 1).",
            )

        for key, level in self.matrix:
            if key.criticality is criticality and key.circuit == circuit:
                return level
        for key, level in self.matrix:
            if key.criticality is criticality and key.circuit is None:
                return level

        raise PolicyNotConfiguredError(
            "service_level_matrix",
            f"No signed service level for criticality={criticality} circuit={circuit}.",
        )


class MaxStockStrategy(BaseModel):
    """Which Max Stock formula applies, and its inputs.

    Unchosen. Solution Design Rule 2 offers EOQ-based, review-period-based, or a
    documented alternative, and requires business sign-off. It also says plainly:
    do NOT hardcode ``Max = 2 x ROP``.

    Neither option is computable today. EOQ needs ordering cost and holding
    rate, which exist in no document and no seeded table; the review-period
    option needs review periods per criticality class, which nobody has
    supplied.
    """

    model_config = ConfigDict(frozen=True)

    strategy: str | None = None
    """``"eoq"``, ``"review_period"``, or another agreed name. Unset."""

    ordering_cost: Decimal | None = None
    """EOQ's ``S``. No source in any document or table."""

    holding_cost_rate: Decimal | None = Field(default=None, ge=0)
    """EOQ's ``H``, a fraction per year. No source."""

    review_period_months: tuple[tuple[Criticality, float], ...] = ()
    """Option B's ``T`` per criticality tier. Empty."""

    @property
    def is_configured(self) -> bool:
        return self.strategy is not None

    def require_configured(self) -> str:
        """Return the chosen strategy, or raise if none has been agreed."""
        if self.strategy is None:
            raise PolicyNotConfiguredError(
                "max_stock_strategy",
                "The Max Stock formula must be agreed with Vedanta before "
                "implementation (Solution Design, Rule 2). Neither EOQ inputs "
                "(ordering cost, holding rate) nor review periods are available.",
            )
        return self.strategy

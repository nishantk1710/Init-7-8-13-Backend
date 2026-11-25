"""Min-Max -> OAR recommendation policy (reverse conversion).

A business rule set of its own. It is deliberately independent of the OAR -> Min-Max
rule (:class:`~app.initiatives.i7.policy.thresholds.ConversionTriggerPolicy`): nothing here
is imported from, derived from or the mirror of it, and the two can move separately.

Every value below is a business decision recorded in the reverse-conversion approval, not an
engineering default. A threshold is changed by editing this policy and bumping
``policy_version``; the version travels with every recommendation and every CSV export so a
result can be tied to the rules that produced it.

What the rule looks at (all per material-plant, all measured up to the *anchor*, the latest
complete month -- see :mod:`app.initiatives.i7.reverse_conversion.metrics`):

* **Issues** -- net issue transactions (issues minus reversals) in the latest 12 months.
* **Months** -- months in those 12 with a *valid movement*: net issued quantity above zero.
* **Last movement** -- how many months before the anchor the latest valid movement was.
* **History span** -- first to latest valid movement month, inclusive.
* **Lifetime months** -- months with a valid movement over all history, up to the anchor.

Stock value never decides a category: it only ranks materials inside one.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.criticality import CriticalityTier

_KNOWN_TIERS = frozenset(tier.value for tier in CriticalityTier)


class ReverseConversionPolicy(BaseModel):
    """The approved Min-Max -> OAR rule, as data."""

    model_config = ConfigDict(frozen=True)

    policy_version: str = "reverse-conversion-v1"
    """Changes whenever any value below changes."""

    # --- Scope -----------------------------------------------------------------
    mrp_types: tuple[str, ...] = ("VM", "VB")
    """Min-Max source MRP types. Blank MRP types are out of scope: they carry no reorder point."""

    plants: tuple[str, ...] = ("1300", "1500")

    # --- Criticality -----------------------------------------------------------
    protected_criticality_tiers: tuple[str, ...] = (
        CriticalityTier.CRITICAL.value,
        CriticalityTier.IMPACT.value,
        CriticalityTier.INSURANCE.value,
    )
    """Never recommended for OAR."""

    obsolete_criticality_tier: str = CriticalityTier.OBSOLETE.value
    """Converts to OAR at Medium confidence, with the reason Obsolete (a planner decides keep or dispose)."""

    # --- Windows ---------------------------------------------------------------
    window_months: int = Field(default=12, ge=1)
    """The latest complete months consumption is measured over."""

    zero_use_window_months: int = Field(default=24, ge=1)
    """No issues across this many months (the latest window and the one before it) is zero use."""

    # --- Eligibility ------------------------------------------------------------
    minimum_history_span_months: int = Field(default=12, ge=1)
    """First to latest valid movement month, inclusive, must reach this."""

    maximum_lead_time_days: int = Field(default=90, ge=1)
    """Above this, or unknown, is Manual Review. No other lead-time restriction applies."""

    # --- Sparse (Recommended) ---------------------------------------------------
    recommended_max_issues: int = Field(default=2, ge=0)
    recommended_max_months: int = Field(default=2, ge=0)
    maximum_months_since_last_movement: int = Field(default=9, ge=0)

    # --- Frequent (Not Eligible) ------------------------------------------------
    frequent_issues_above: int = Field(default=4, ge=0)
    frequent_months_above: int = Field(default=4, ge=0)

    # --- Lifetime criterion -----------------------------------------------------
    new_material_age_months: int = Field(default=24, ge=1)
    """A material whose first valid movement is within this many months of the anchor is new."""

    new_material_minimum_lifetime_months: int = Field(default=3, ge=0)
    """A new material needs this many months with use to be Recommended; below it, Manual Review."""

    @field_validator("protected_criticality_tiers")
    @classmethod
    def _known_protected(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # A misspelt tier would protect nothing and, silently, send protected materials down the
        # Recommended path -- so refuse it at construction.
        unknown = [tier for tier in value if tier not in _KNOWN_TIERS]
        if unknown:
            raise ValueError(f"unknown criticality tier(s) {unknown}; expected some of {sorted(_KNOWN_TIERS)}")
        return value

    @field_validator("obsolete_criticality_tier")
    @classmethod
    def _known_obsolete(cls, value: str) -> str:
        if value not in _KNOWN_TIERS:
            raise ValueError(f"unknown criticality tier {value!r}; expected one of {sorted(_KNOWN_TIERS)}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "ReverseConversionPolicy":
        if self.obsolete_criticality_tier in self.protected_criticality_tiers:
            raise ValueError("the obsolete tier cannot also be a protected tier")
        if self.recommended_max_issues > self.frequent_issues_above:
            raise ValueError("recommended_max_issues must not exceed frequent_issues_above")
        if self.recommended_max_months > self.frequent_months_above:
            raise ValueError("recommended_max_months must not exceed frequent_months_above")
        if self.zero_use_window_months != 2 * self.window_months:
            # Zero use is read from the latest window and the one before it, and nothing else.
            raise ValueError("zero_use_window_months must be exactly twice window_months")
        if self.maximum_months_since_last_movement >= self.window_months:
            raise ValueError("maximum_months_since_last_movement must be inside the latest window")
        return self

    def parameters(self) -> dict[str, Any]:
        """The applied thresholds, as one flat mapping for an API response or an export header."""
        return self.model_dump(mode="json")


def current_reverse_conversion_policy() -> ReverseConversionPolicy:
    """The rule in force.

    Matches the approved decision table: sparse = at most 2 issues and 2 months with use in the
    latest 12 months and a last movement within 9 months; frequent = more than 4 issues or more
    than 4 months; lead time over 90 days or unknown goes to Manual Review; a new material needs at
    least 3 lifetime months with use to be Recommended.
    """
    return ReverseConversionPolicy()

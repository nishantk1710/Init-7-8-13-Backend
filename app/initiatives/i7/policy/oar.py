"""OAR identification policy.

**One condition (21 Nov 2026).** A material-plant is OAR when its MRP type
(``MARC.DISMM``) is in the configured list -- ``ND`` and ``PD`` by default. The
MRP controller (``MARC.DISPO``) plays no part in the rule any more, and a blank
or missing MRP type is **not** OAR (``blank_means_out_of_scope``), like any code
not in the list.

The codes are configuration, not a hard-coded rule: they come from
``Settings.i7_oar_mrp_types``, I07's own setting, so the business can retune
I07's ruling without a code change and without touching I13's.

The plant scope (1300 and 1500, ``I7_PLANTS``) and the deletion exclusion
(LVORM = X at plant or client level) are not predicates here: they are applied
earlier, at ingestion and in the feature-store build, so a pair never reaches
this rule unless it is in scope and not deleted.

MRP Type ``VB`` is normal Min-Max managed; ``ND`` and ``PD`` are OAR. The six
undocumented codes (V1, M0, RP, VI, VH, V2) are not OAR either.

Two documents previously disagreed with this (FRS v1.3: ``MARA.EXTWG = 100``;
an earlier checked-in rule: ``DISMM in {ND, PD} AND MSTAE != '01'``). The
MSTAE/material-status exclusion has been removed from the active rule, and
``material_status`` stays on the contract and feature store as provenance only.

**EXTWG is retired.** It stays on the canonical contract for source fidelity,
but a predicate may not name it -- :meth:`OarPolicy.validate` refuses -- so the
retirement is enforced rather than merely documented.

**Still not confirmed on roll-up.** ``confirmed`` defaults to ``False`` and
the roll-up policy defaults to unset. Roll-up across a material's plants is a
team-lead call this module still does not make.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import get_settings
from app.initiatives.i7.contracts.enums import ScopeDecision
from app.initiatives.i7.contracts.material import MaterialAttributes
from app.initiatives.i7.errors import ConfigurationError

RETIRED_FIELDS = frozenset({"external_material_group"})
"""Fields no active rule may use. EXTWG was retired as an OAR identifier; it
remains on the contract for audit, and this set stops it coming back in through
configuration."""


class PredicateField(StrEnum):
    """Contract fields an OAR predicate may read.

    A closed set on purpose. It keeps configuration honest -- a typo is a load
    error, not a rule that silently matches nothing -- and it is where the EXTWG
    retirement is enforced, since the field simply is not offered.
    """

    MRP_TYPE = "mrp_type"
    MRP_CONTROLLER = "mrp_controller"
    MATERIAL_STATUS = "material_status"
    MATERIAL_GROUP = "material_group"


class PredicateOperator(StrEnum):
    IN = "in"
    NOT_IN = "not_in"
    EQUALS = "eq"
    NOT_EQUALS = "ne"
    STARTS_WITH = "starts_with"


class RollupPolicy(StrEnum):
    """How per-plant answers combine into a material-level answer.

    Unset by default. ``DISMM`` is per material-plant, so a material can be OAR
    at one plant and not another; which way that rolls up is a business call
    that has not been made.
    """

    PER_PLANT_ONLY = "per-plant-only"
    ANY_PLANT = "any-plant"
    ALL_PLANTS = "all-plants"


class ScopePredicate(BaseModel):
    """One condition on one field."""

    model_config = ConfigDict(frozen=True)

    field: PredicateField
    operator: PredicateOperator
    values: tuple[str, ...] = ()
    unknown_values: tuple[str, ...] = ("",)
    """Raw values meaning "not maintained". A blank string by default, so an
    empty DISMM is UNKNOWN rather than OUT_OF_SCOPE. Note this only covers a
    *string* blank -- the staging adapter's own ``clean()`` collapses a blank
    cell to Python ``None`` before it ever reaches here (blank and NULL are
    the same fact), so in practice ``raw is None`` (handled separately below)
    is what a real unmaintained field looks like, not a value in this tuple."""

    blank_means_in_scope: bool = False
    """Business-confirmed exception, OAR-specific: when set, a genuinely
    unmaintained field (``raw is None``) evaluates IN_SCOPE instead of
    UNKNOWN. Off by default -- most predicates should still treat "not
    maintained" as "not known", per ScopeDecision's own three-state design;
    this exists because the OAR rule specifically was confirmed the other
    way (see oar.py's module docstring)."""

    blank_means_out_of_scope: bool = False
    """The opposite exception, for a field whose *presence* is the evidence --
    the MRP controller (DISPO): a material is OAR because its controller says
    ``OAR``, so no controller is no evidence, and evaluates OUT_OF_SCOPE rather
    than UNKNOWN. Covers both a missing value (``None``, as the staging adapter
    collapses a blank cell) and a whitespace-only string. Off by default, and
    never together with ``blank_means_in_scope``."""

    @model_validator(mode="after")
    def _values_match_operator(self) -> "ScopePredicate":
        if self.blank_means_in_scope and self.blank_means_out_of_scope:
            raise ValueError(
                f"predicate on {self.field} cannot treat blank as both in and out of scope"
            )
        if not self.values:
            raise ValueError(f"predicate on {self.field} needs at least one value")
        single = {PredicateOperator.EQUALS, PredicateOperator.NOT_EQUALS, PredicateOperator.STARTS_WITH}
        if self.operator in single and len(self.values) != 1:
            raise ValueError(f"operator {self.operator} takes exactly one value")
        return self

    def evaluate(self, attributes: MaterialAttributes) -> ScopeDecision:
        """Apply this predicate to one material-plant."""
        raw = getattr(attributes, self.field.value)
        if self.blank_means_out_of_scope and (raw is None or not raw.strip()):
            return ScopeDecision.OUT_OF_SCOPE
        if raw is None:
            return ScopeDecision.IN_SCOPE if self.blank_means_in_scope else ScopeDecision.UNKNOWN
        if raw in self.unknown_values:
            return ScopeDecision.UNKNOWN

        matched = self._matches(raw)
        return ScopeDecision.IN_SCOPE if matched else ScopeDecision.OUT_OF_SCOPE

    def _matches(self, raw: str) -> bool:
        match self.operator:
            case PredicateOperator.IN:
                return raw in self.values
            case PredicateOperator.NOT_IN:
                return raw not in self.values
            case PredicateOperator.EQUALS:
                return raw == self.values[0]
            case PredicateOperator.NOT_EQUALS:
                return raw != self.values[0]
            case PredicateOperator.STARTS_WITH:
                return raw.startswith(self.values[0])
        raise ConfigurationError(f"unhandled operator {self.operator}")


class OarPolicy(BaseModel):
    """Predicates combined by AND, evaluated per material-plant."""

    model_config = ConfigDict(frozen=True)

    predicates: tuple[ScopePredicate, ...] = Field(min_length=1)
    rollup: RollupPolicy | None = None
    """Unset until the team lead rules."""

    confirmed: bool = False
    """Whether the business has signed this rule off. ``False`` today."""

    notes: str = ""

    @field_validator("predicates")
    @classmethod
    def _no_retired_fields(cls, predicates: tuple[ScopePredicate, ...]) -> tuple[ScopePredicate, ...]:
        for predicate in predicates:
            if predicate.field.value in RETIRED_FIELDS:
                raise ValueError(
                    f"{predicate.field.value} is retired as an OAR identifier and "
                    "must not appear in an active rule"
                )
        return predicates

    def evaluate(self, attributes: MaterialAttributes) -> ScopeDecision:
        """AND the predicates for one material-plant.

        Order of precedence, and why:

        * any predicate OUT_OF_SCOPE wins -- a conjunction is already false, and
          an obsolete material is out regardless of what MRP type says;
        * otherwise any UNKNOWN wins -- the answer genuinely is not known;
        * otherwise IN_SCOPE.
        """
        decisions = [predicate.evaluate(attributes) for predicate in self.predicates]
        if any(decision is ScopeDecision.OUT_OF_SCOPE for decision in decisions):
            return ScopeDecision.OUT_OF_SCOPE
        if any(decision is ScopeDecision.UNKNOWN for decision in decisions):
            return ScopeDecision.UNKNOWN
        return ScopeDecision.IN_SCOPE


def current_oar_policy() -> OarPolicy:
    """The rule in force: ``MRP_TYPE in Settings.i7_oar_mrp_types`` (``ND``, ``PD``).

    The codes are read from I07's own setting, so a retuned list moves I07 only.
    ``blank_means_out_of_scope=True`` makes a blank or missing MRP type not OAR:
    the staging adapter's ``clean()`` collapses an empty DISMM cell to Python
    ``None``, so a real blank material-plant reaches the predicate as
    ``raw is None``.

    Ships unconfirmed on roll-up only. Changing the rule means changing the
    setting or supplying a different policy -- no business logic elsewhere names
    an MRP type.
    """
    return OarPolicy(
        predicates=(
            ScopePredicate(
                field=PredicateField.MRP_TYPE,
                operator=PredicateOperator.IN,
                values=tuple(sorted(get_settings().i7_oar_mrp_type_set)),
                blank_means_out_of_scope=True,
            ),
        ),
        rollup=None,
        confirmed=False,
        notes=(
            "MRP type (DISMM) in the configured list (ND, PD) is OAR -- the I13 "
            "ruling of 2026-11-21, applied to I07 through its own setting. A blank "
            "MRP type is not OAR; DISPO is not read. Roll-up policy still pending "
            "team-lead confirmation."
        ),
    )

"""Service level and the Z factor -- the business-policy gate.

Formula Reference, Stage 4::

    Z = Phi^-1(service_level)        scipy.stats.norm.ppf

The arithmetic is trivial. The gate is not.

The Solution Design's Rule 1 could hardly be more explicit::

    THIS MUST COME FROM VEDANTA, NOT FROM CODE
    Do NOT invent percentages. Do NOT assume 98% for critical.
    If policy is unsigned -> system blocks recommendations

So when the matrix is unsigned this returns ``NOT_EVALUABLE_SERVICE_LEVEL_UNSET``
with a null Z, and every downstream calculation blocks. That is the designed
behaviour, not a limitation to work around: an invented service level does not
fail loudly, it produces a safety stock that looks entirely reasonable, cites a
Z factor nobody approved, and gets signed off.

The percentages in the Formula Reference's Z table (85%, 90%, ... 99.5%) are
mathematical illustrations of ``norm.ppf``. They are used in tests to verify the
transformation and never as business configuration.
"""

from decimal import Decimal

from app.initiatives.i7.contracts import Criticality
from app.initiatives.i7.errors import PolicyNotConfiguredError
from app.initiatives.i7.inventory.types import CalculationStatus, ServiceLevelResult
from app.initiatives.i7.policy import ServiceLevelPolicy


def z_factor(service_level: Decimal) -> Decimal:
    """``Phi^-1(service_level)`` -- the standard normal inverse CDF.

    Raises ``ValueError`` outside ``(0, 1)``: ``norm.ppf(0)`` is minus infinity
    and ``norm.ppf(1)`` is infinity, and either would propagate silently through
    the safety-stock formula as a non-finite quantity.
    """
    from scipy.stats import norm

    level = float(service_level)
    if not 0 < level < 1:
        raise ValueError(
            f"service level must lie strictly between 0 and 1, got {level}"
        )

    value = float(norm.ppf(level))
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"norm.ppf({level}) is not finite")

    return Decimal(str(round(value, 6)))


def resolve(
    policy: ServiceLevelPolicy,
    criticality: Criticality | None,
    circuit: str | None,
) -> ServiceLevelResult:
    """Look up the signed service level and derive Z.

    Blocks rather than defaulting at every failure point: unsigned matrix,
    unknown criticality, or a combination the matrix does not cover.
    """
    if not policy.is_configured:
        return ServiceLevelResult(
            status=CalculationStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET,
            criticality=criticality.value if criticality else None,
            circuit=circuit,
            detail=(
                "the Criticality x Circuit service-level matrix must be supplied "
                "and signed by Vedanta (Solution Design, Rule 1); recommendations "
                "are blocked until then"
            ),
        )

    if criticality is None:
        # Criticality is the matrix key. Without it there is nothing to look up,
        # and defaulting to a tier would pick a service level by accident.
        return ServiceLevelResult(
            status=CalculationStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET,
            circuit=circuit,
            detail="no criticality tier, so no service level can be looked up",
        )

    try:
        level = Decimal(str(policy.service_level_for(criticality, circuit)))
    except PolicyNotConfiguredError as exc:
        return ServiceLevelResult(
            status=CalculationStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET,
            criticality=criticality.value,
            circuit=circuit,
            detail=str(exc),
        )

    try:
        factor = z_factor(level)
    except ValueError as exc:
        return ServiceLevelResult(
            status=CalculationStatus.CALCULATION_ERROR,
            service_level=level,
            criticality=criticality.value,
            circuit=circuit,
            detail=str(exc),
        )

    return ServiceLevelResult(
        status=CalculationStatus.SUCCESS,
        service_level=level,
        z_factor=factor,
        criticality=criticality.value,
        circuit=circuit,
    )

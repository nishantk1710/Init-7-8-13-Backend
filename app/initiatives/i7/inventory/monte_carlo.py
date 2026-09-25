"""Monte Carlo safety stock for LUMPY demand.

Formula Reference, Stage 5 Path B::

    repeat 10,000 times:
        1. sample N ~ Poisson(lambda)
        2. sample N demand sizes from the historical non-zero distribution
        3. sum -> one simulated lead-time demand

    SS = Quantile(service_level, simulated_LTD) - E[LTD]

Why simulate at all: the compound-Poisson normal approximation assumes the
lead-time demand distribution is roughly symmetric. For lumpy demand it is not
-- a few very large orders give it a long right tail, and the tail is exactly
where a service-level quantile is read. Simulating the sum directly gets the
quantile from the real shape instead of a bell curve fitted to it.

**Sizes are sampled only from observed non-zero demand.** Never from zeros
(which are the absence of an event, not a small one), never from forecasts, and
never from a fitted distribution -- the empirical distribution is the evidence.

**Deterministic.** The seed is derived from the material, plant and formula
version, so the same inputs always give the same answer. A timestamp seed would
make a recommendation irreproducible the moment anyone asked how it was reached.
"""

import hashlib
from decimal import ROUND_CEILING, Decimal

from app.initiatives.i7.inventory.types import (
    FORMULA_VERSION,
    CalculationStatus,
    SafetyStockResult,
)

SIMULATION_COUNT = 10_000
"""The documented count."""

MINIMUM_NON_ZERO_OBSERVATIONS = 2
"""One observed size is not a distribution to sample from."""


def derive_seed(material: str, plant: str, formula_version: str = FORMULA_VERSION) -> int:
    """A stable seed from the calculation's identity.

    Hashed rather than combined arithmetically so that neighbouring materials
    get unrelated streams, and stable across processes -- Python's own ``hash``
    is randomised per interpreter run, which would break reproducibility in the
    least visible way possible.
    """
    digest = hashlib.sha256(f"{material}|{plant}|{formula_version}".encode()).digest()
    return int.from_bytes(digest[:4], "big")


def simulate(
    service_level: Decimal,
    lt_avg_months: Decimal,
    p_final: Decimal,
    non_zero_demand: list[Decimal],
    material: str,
    plant: str,
    simulations: int = SIMULATION_COUNT,
) -> SafetyStockResult:
    """Safety stock from the simulated lead-time demand distribution."""
    if p_final <= 0:
        return SafetyStockResult(
            status=CalculationStatus.NOT_EVALUABLE_INSUFFICIENT_DEMAND,
            method="monte_carlo",
            detail="smoothed demand interval is zero or negative, so lambda is undefined",
        )

    if len(non_zero_demand) < MINIMUM_NON_ZERO_OBSERVATIONS:
        return SafetyStockResult(
            status=CalculationStatus.NOT_EVALUABLE_INSUFFICIENT_DEMAND,
            method="monte_carlo",
            detail=(
                f"{len(non_zero_demand)} non-zero observation(s); "
                f"{MINIMUM_NON_ZERO_OBSERVATIONS} needed to sample a distribution"
            ),
        )

    lambda_events = float(lt_avg_months / p_final)
    if lambda_events <= 0:
        return SafetyStockResult(
            status=CalculationStatus.NOT_EVALUABLE_INSUFFICIENT_DEMAND,
            method="monte_carlo",
            detail="expected demand events during the lead time is zero or negative",
        )

    import numpy as np

    seed = derive_seed(material, plant)
    generator = np.random.default_rng(seed)
    sizes = np.array([float(value) for value in non_zero_demand], dtype=float)

    # Vectorised: draw every event count at once, then draw the total number of
    # demand sizes needed in one call and split by count. Ten thousand separate
    # draws per material would not finish in reasonable time across a catalogue.
    counts = generator.poisson(lambda_events, size=simulations)
    total_events = int(counts.sum())

    if total_events == 0:
        # Lambda so small that no simulation drew an event. Lead-time demand is
        # zero throughout, so the quantile and the mean coincide and safety
        # stock is genuinely zero -- a real result, not a missing one.
        return SafetyStockResult(
            status=CalculationStatus.SUCCESS,
            method="monte_carlo",
            raw_safety_stock=Decimal(0),
            safety_stock=0,
            trace=(
                ("simulation_count", str(simulations)),
                ("random_seed", str(seed)),
                ("lambda", str(lambda_events)),
                ("simulated_quantile", "0"),
                ("simulated_mean", "0"),
                ("raw_safety_stock", "0"),
                ("rounding_method", "CEILING"),
            ),
            detail="no demand events were simulated at this lambda",
        )

    draws = generator.choice(sizes, size=total_events, replace=True)
    # Segment the flat draw array back into per-simulation sums.
    boundaries = np.concatenate(([0], np.cumsum(counts)))
    cumulative = np.concatenate(([0.0], np.cumsum(draws)))
    totals = cumulative[boundaries[1:]] - cumulative[boundaries[:-1]]

    quantile = float(np.quantile(totals, float(service_level)))
    expected = float(totals.mean())
    raw = Decimal(str(round(quantile - expected, 6)))

    if raw < 0:
        # The quantile sits below the mean only for a service level under 50%,
        # which is not a stocking policy. Surfaced rather than clamped.
        return SafetyStockResult(
            status=CalculationStatus.CALCULATION_ERROR,
            method="monte_carlo",
            raw_safety_stock=raw,
            detail=(
                "simulated quantile fell below the simulated mean; a service "
                "level below 50% is not a valid stocking target"
            ),
        )

    return SafetyStockResult(
        status=CalculationStatus.SUCCESS,
        method="monte_carlo",
        raw_safety_stock=raw,
        safety_stock=int(raw.to_integral_value(rounding=ROUND_CEILING)),
        trace=(
            ("simulation_count", str(simulations)),
            ("random_seed", str(seed)),
            ("lambda", str(lambda_events)),
            ("service_level", str(service_level)),
            ("simulated_quantile", str(round(quantile, 6))),
            ("simulated_mean", str(round(expected, 6))),
            ("raw_safety_stock", str(raw)),
            ("rounding_method", "CEILING"),
        ),
    )

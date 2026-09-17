"""Maximum stock -- pluggable strategies behind an unsigned policy gate.

Solution Design, Rule 2::

    THE MAX STOCK FORMULA MUST BE AGREED DURING SOLUTION DESIGN
    Do NOT hardcode Max = 2 x ROP.
    Do NOT pick a formula without business sign-off.
    The formula choice is configuration, not a coding decision.

So all three candidate formulas are implemented and none is selected. With no
signed strategy the engine returns ``NOT_CONFIGURED`` and a null maximum.

Neither option is computable on the current data even if one were chosen:

* **EOQ** needs an ordering cost ``S`` and an annual holding rate ``H``, which
  appear in no I07 document and no seeded table.
* **Review-period** needs an agreed review period ``T`` per criticality class,
  which nobody has supplied.

That is why they are written as strategies with explicit input checks rather
than left unwritten -- the arithmetic is verified by tests now, and signing a
policy later is a configuration change rather than new code.
"""

from decimal import ROUND_CEILING, Decimal
from typing import NamedTuple, Protocol

from app.initiatives.i7.inventory.types import CalculationStatus, MaxStockResult
from app.initiatives.i7.policy import MaxStockStrategy as MaxStockPolicy


class MaxStockContext(NamedTuple):
    """Everything any strategy might need."""

    safety_stock: int | None
    rop: int | None
    forecast_rate: Decimal | None
    criticality: str | None
    unit_price: Decimal | None
    ordering_cost: Decimal | None
    holding_cost_rate: Decimal | None
    review_period_months: Decimal | None


class MaxStockStrategy(Protocol):
    """One way of computing a maximum stock level."""

    name: str

    def calculate(self, context: MaxStockContext) -> MaxStockResult: ...


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


class NotConfiguredMaxStockStrategy:
    """The active strategy: none has been signed.

    Returns a status, never a number. There is deliberately no ``2 x ROP``
    fallback anywhere in this module -- the Solution Design prohibits it by name,
    and a plausible default is exactly what would survive review unnoticed.
    """

    name = "not_configured"

    def calculate(self, context: MaxStockContext) -> MaxStockResult:
        return MaxStockResult(
            status=CalculationStatus.NOT_CONFIGURED,
            detail=(
                "the Max Stock formula must be agreed with Vedanta before "
                "implementation (Solution Design, Rule 2); no strategy is signed"
            ),
        )


class EoqMaxStockStrategy:
    """Option A::

        EOQ = sqrt( 2 * D_annual * S / (H * P) )
        Max = SS + EOQ

    with ``D_annual = forecast_rate * 12``.
    """

    name = "eoq"

    def calculate(self, context: MaxStockContext) -> MaxStockResult:
        import math

        missing = [
            label
            for label, value in (
                ("safety_stock", context.safety_stock),
                ("forecast_rate", context.forecast_rate),
                ("ordering_cost", context.ordering_cost),
                ("holding_cost_rate", context.holding_cost_rate),
                ("unit_price", context.unit_price),
            )
            if value is None
        ]
        if missing:
            return MaxStockResult(
                status=CalculationStatus.NOT_EVALUABLE_COST_DATA,
                strategy=self.name,
                detail=f"EOQ requires {', '.join(missing)}",
            )

        denominator = context.holding_cost_rate * context.unit_price
        if denominator <= 0:
            return MaxStockResult(
                status=CalculationStatus.NOT_EVALUABLE_COST_DATA,
                strategy=self.name,
                detail="holding rate times unit price must be positive",
            )

        annual_demand = context.forecast_rate * Decimal(12)
        eoq_squared = (Decimal(2) * annual_demand * context.ordering_cost) / denominator
        if eoq_squared < 0:
            return MaxStockResult(
                status=CalculationStatus.CALCULATION_ERROR,
                strategy=self.name,
                detail="negative EOQ term",
            )

        eoq = Decimal(str(math.sqrt(float(eoq_squared))))
        raw = Decimal(context.safety_stock) + eoq

        return MaxStockResult(
            status=CalculationStatus.SUCCESS,
            strategy=self.name,
            raw_max_stock=raw,
            max_stock=_ceil(raw),
            trace=(
                ("strategy", self.name),
                ("d_annual", str(annual_demand)),
                ("ordering_cost", str(context.ordering_cost)),
                ("holding_cost_rate", str(context.holding_cost_rate)),
                ("unit_price", str(context.unit_price)),
                ("eoq", str(eoq)),
                ("safety_stock", str(context.safety_stock)),
                ("raw_max_stock", str(raw)),
                ("rounding_method", "CEILING"),
            ),
        )


class ReviewPeriodMaxStockStrategy:
    """Option B::

        Max = ROP + (D_rate * T)
    """

    name = "review_period"

    def calculate(self, context: MaxStockContext) -> MaxStockResult:
        if context.review_period_months is None:
            return MaxStockResult(
                status=CalculationStatus.NOT_CONFIGURED,
                strategy=self.name,
                detail="no agreed review period T for this criticality class",
            )
        missing = [
            label
            for label, value in (
                ("rop", context.rop),
                ("forecast_rate", context.forecast_rate),
            )
            if value is None
        ]
        if missing:
            return MaxStockResult(
                status=CalculationStatus.NOT_EVALUABLE_INVALID_FORECAST,
                strategy=self.name,
                detail=f"review-period strategy requires {', '.join(missing)}",
            )
        if context.review_period_months <= 0:
            return MaxStockResult(
                status=CalculationStatus.NOT_CONFIGURED,
                strategy=self.name,
                detail="review period must be positive",
            )

        coverage = context.forecast_rate * context.review_period_months
        raw = Decimal(context.rop) + coverage

        return MaxStockResult(
            status=CalculationStatus.SUCCESS,
            strategy=self.name,
            raw_max_stock=raw,
            max_stock=_ceil(raw),
            trace=(
                ("strategy", self.name),
                ("rop", str(context.rop)),
                ("forecast_rate", str(context.forecast_rate)),
                ("review_period_months", str(context.review_period_months)),
                ("review_period_coverage", str(coverage)),
                ("raw_max_stock", str(raw)),
                ("rounding_method", "CEILING"),
            ),
        )


STRATEGIES: dict[str, MaxStockStrategy] = {
    EoqMaxStockStrategy.name: EoqMaxStockStrategy(),
    ReviewPeriodMaxStockStrategy.name: ReviewPeriodMaxStockStrategy(),
}
"""Implemented strategies, by the name a policy would select.

Option C (hybrid) is absent: the Solution Design allows it only once a future
approved policy defines it, and there is nothing yet to implement.
"""


def strategy_for(policy: MaxStockPolicy) -> MaxStockStrategy:
    """The configured strategy, or the not-configured one.

    An unrecognised name also yields not-configured rather than falling back to
    a working formula -- a typo in configuration must not silently produce
    numbers under the wrong policy.
    """
    if not policy.is_configured or policy.strategy is None:
        return NotConfiguredMaxStockStrategy()
    return STRATEGIES.get(policy.strategy, NotConfiguredMaxStockStrategy())

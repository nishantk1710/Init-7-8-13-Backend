"""Prepared demand series for forecasting.

Consumes the Phase 2/3 canonical :class:`ConsumptionSeries`. Nothing here reads
``raw_*`` or reconstructs demand from SAP tables -- the series arrives already
densified over the extract's observation window, with zero-demand months
present, which is what every model below depends on.

Validation is defensive rather than corrective. A series that violates an
invariant is rejected with a reason; it is never repaired by dropping or
interpolating points, because both would change the intermittency the models
exist to measure.
"""

from datetime import date
from decimal import Decimal
from typing import NamedTuple

from app.initiatives.i7.contracts import ConsumptionSeries
from app.initiatives.i7.forecasting.types import DemandPoint


class SeriesProblem(NamedTuple):
    """Why a series cannot be forecast."""

    reason: str
    detail: str


class PreparedSeries(NamedTuple):
    """A validated monthly demand series, ready for any model."""

    points: tuple[DemandPoint, ...]
    unit: str | None

    @property
    def values(self) -> list[Decimal]:
        return [point.quantity for point in self.points]

    @property
    def periods(self) -> list[date]:
        return [point.period for point in self.points]

    @property
    def length(self) -> int:
        return len(self.points)

    @property
    def non_zero_count(self) -> int:
        return sum(1 for point in self.points if point.quantity > 0)

    def through(self, index: int) -> "PreparedSeries":
        """The first ``index`` points -- the training window at an origin.

        The single mechanism by which models see history. A model that only ever
        receives ``through(i)`` cannot look ahead, which is what makes the
        leakage guarantee structural rather than a matter of discipline.
        """
        return PreparedSeries(points=self.points[:index], unit=self.unit)


def prepare(series: ConsumptionSeries) -> tuple[PreparedSeries | None, SeriesProblem | None]:
    """Validate a canonical series. Returns ``(prepared, None)`` or ``(None, problem)``.

    The contract already enforces contiguity and ordering, so these checks are
    belt and braces -- but they are cheap, and a silently mis-ordered series
    would corrupt every model downstream in a way no metric would reveal.
    """
    if not series.observations:
        return None, SeriesProblem("empty_series", "no observations")

    points: list[DemandPoint] = []
    seen: set[date] = set()
    previous: date | None = None

    for observation in series.observations:
        period = observation.period
        if period in seen:
            return None, SeriesProblem("duplicate_period", f"{period} appears twice")
        if previous is not None and period <= previous:
            return None, SeriesProblem(
                "out_of_order", f"{period} does not follow {previous}"
            )
        if observation.quantity < 0:
            return None, SeriesProblem(
                "negative_demand", f"{period} has quantity {observation.quantity}"
            )
        seen.add(period)
        previous = period
        points.append(DemandPoint(period=period, quantity=observation.quantity))

    unit = next(
        (observation.unit_of_measure for observation in series.observations
         if observation.unit_of_measure),
        None,
    )
    return PreparedSeries(points=tuple(points), unit=unit), None


def add_months(period: date, months: int) -> date:
    """Advance a first-of-month date. Used to label forecast periods."""
    total = period.month - 1 + months
    return date(period.year + total // 12, total % 12 + 1, 1)

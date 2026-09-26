"""Canonical consumption history.

ADI, CV-squared and every downstream statistic depend on one thing being right:
a month with no demand must be present in the series as a zero, not missing.

    ADI = n / n_nz

``n`` counts *all* periods and ``n_nz`` only non-zero ones, so dropping empty
months shrinks ``n``, drags ADI toward 1.0, and reclassifies intermittent
materials as smooth. The same zeros belong in sigma_D, which the Formula
Reference states explicitly: "Include zeros -- they represent real zero-demand
months."

So :class:`ConsumptionSeries` models a *dense* monthly series and validates that
it is contiguous. A gap is rejected rather than silently tolerated, because a
gap is indistinguishable from a zero once it reaches the arithmetic, and the
resulting misclassification is invisible.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.initiatives.i7.contracts.identity import MaterialPlantKey


def _next_month(period: date) -> date:
    """First day of the month after ``period``."""
    if period.month == 12:
        return date(period.year + 1, 1, 1)
    return date(period.year, period.month + 1, 1)


class ConsumptionObservation(BaseModel):
    """Demand for one material-plant in one month.

    A zero quantity is a valid, meaningful observation -- the whole point of
    intermittent-demand modelling -- so nothing here treats zero as empty.
    """

    model_config = ConfigDict(frozen=True)

    period: date
    """First day of the month the demand falls in, normalised by the validator."""

    quantity: Decimal = Field(ge=0)
    """Issued quantity, always non-negative.

    Reversals (SAP movement types 202/262) net against the issues they reverse
    during aggregation in the adapter; a period total is never negative."""

    unit_of_measure: str | None = None

    @model_validator(mode="after")
    def _normalise_period(self) -> "ConsumptionObservation":
        if self.period.day != 1:
            object.__setattr__(self, "period", self.period.replace(day=1))
        return self

    @property
    def is_zero_demand(self) -> bool:
        return self.quantity == 0


class ConsumptionSeries(BaseModel):
    """A contiguous monthly series for one material-plant."""

    model_config = ConfigDict(frozen=True)

    key: MaterialPlantKey
    observations: tuple[ConsumptionObservation, ...]

    @model_validator(mode="after")
    def _contiguous_and_ordered(self) -> "ConsumptionSeries":
        periods = [observation.period for observation in self.observations]
        if len(set(periods)) != len(periods):
            raise ValueError("duplicate period in consumption series")
        for earlier, later in zip(periods, periods[1:]):
            if later <= earlier:
                raise ValueError("consumption observations must be in ascending period order")
            if later != _next_month(earlier):
                raise ValueError(
                    f"gap in consumption series between {earlier} and {later}: "
                    "zero-demand months must be present explicitly, because ADI "
                    "counts total periods and a missing month is silently "
                    "indistinguishable from a zero one"
                )
        return self

    @property
    def total_periods(self) -> int:
        """``n`` -- every month, zeros included."""
        return len(self.observations)

    @property
    def non_zero_periods(self) -> int:
        """``n_nz`` -- months with demand."""
        return sum(1 for observation in self.observations if not observation.is_zero_demand)

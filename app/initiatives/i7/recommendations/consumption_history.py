"""The monthly consumption a recommendation's detail shows next to its numbers.

This is the I07 service the API reads through. The API layer never imports the
staging adapter (``test_nothing_outside_i07_imports_the_staging_adapter``); this
module is inside I07, so it may, and it re-uses the adapter's series rather than
building a second one.

It is the series forecasting and classification see:

* **Complete over the extract's window.** A month with no movement is an explicit
  zero, so a chart draws "no demand that month" instead of joining two distant
  points with a line.
* **Netted, then floored at zero.** Staging nets issues (+) against reversals (-)
  per month (``ingestion_policy``: movement types 201/261 issue, 202/262 reverse).
  A month goes negative when a reversal posts after the issue it cancels, which
  sits in an earlier month. Demand is not negative, so such a month is zero -- the
  same rule ``features.builder._densify`` applies before ADI, CV-squared, the
  forecast and the declining-demand test. The chart agreeing with that rule is the
  point: it shows the history the models were given. (The cancelled quantity is
  not carried back to the earlier month there either; that is the existing
  convention, not something this view changes.)
* **Empty, never a guess,** for a material-plant with no staged consumption.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.initiatives.i7.adapters.repository import consumption_series_for

DETAIL_HISTORY_MONTHS = 24
"""How many of the latest months the detail carries."""


def monthly_consumption_history(
    session: Session, material: str, plant: str, *, months: int = DETAIL_HISTORY_MONTHS
) -> tuple[tuple[date, Decimal], ...]:
    """``(month, quantity)`` for the latest ``months`` months, oldest first."""
    series = consumption_series_for(session, material, plant)
    if series is None:
        return ()
    return tuple((observation.period, observation.quantity) for observation in series.observations[-months:])

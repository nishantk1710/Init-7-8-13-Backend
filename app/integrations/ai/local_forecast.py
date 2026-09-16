"""In-process demand forecasting, behind the Forecaster port.

Deliberately minimal. I07's real engine is W4.2 -- Croston, SBA and an ML
challenger selected by backtest -- and it does not depend on W1.5 at all,
because those are statistics running in this process with no provider to
abstract.

So this is not that engine. It is the port's first implementation: enough to
prove the interface is usable and to give callers something working, with the
seam in place should forecasting later move to a hosted endpoint.

Croston's method, briefly: spare-parts demand is intermittent -- long runs of
zeros with occasional spikes -- and a plain moving average smears those spikes
into a fictional trickle. Croston instead tracks two things separately, the size
of a demand when it happens and the gap between demands, then divides one by the
other. That is the accepted treatment for this shape of demand and it is what
the I07 FRS specifies.
"""

from __future__ import annotations

from typing import Sequence

from app.core.ai import Forecast, Forecaster, ForecastPoint

# Smoothing constant. 0.1 is the conventional starting point for intermittent
# demand; W4.2 will tune this against backtest results.
DEFAULT_ALPHA = 0.1


class CrostonForecaster(Forecaster):
    """Croston's method for intermittent demand."""

    name = "croston"

    def __init__(self, alpha: float = DEFAULT_ALPHA) -> None:
        if not 0 < alpha <= 1:
            raise ValueError(f"alpha must be in (0, 1], got {alpha}")
        self._alpha = alpha

    def forecast(self, history: Sequence[float], *, horizon: int = 1) -> Forecast:
        if horizon < 1:
            raise ValueError(f"horizon must be at least 1, got {horizon}")

        demands = [(i, q) for i, q in enumerate(history) if q > 0]

        if not demands:
            # No demand ever recorded. Zero is the honest answer; inventing a
            # small positive figure would put safety stock on a dead part.
            return Forecast(
                points=[ForecastPoint(period=str(i + 1), quantity=0.0) for i in range(horizon)],
                method=self.name,
                horizon=horizon,
                detail={"reason": "no non-zero demand in the series"},
            )

        # Size of a demand when one occurs, and the interval between occurrences.
        size = float(demands[0][1])
        interval = float(demands[0][0] + 1)

        for (previous_index, _), (index, quantity) in zip(demands, demands[1:]):
            size += self._alpha * (quantity - size)
            interval += self._alpha * ((index - previous_index) - interval)

        rate = size / interval if interval else 0.0
        return Forecast(
            points=[
                ForecastPoint(period=str(i + 1), quantity=round(rate, 4))
                for i in range(horizon)
            ],
            method=self.name,
            horizon=horizon,
            detail={
                "alpha": self._alpha,
                "demand_size": round(size, 4),
                "demand_interval": round(interval, 4),
                "periods": len(history),
                "non_zero_periods": len(demands),
            },
        )

    def check_connection(self) -> None:
        """Nothing to reach -- this runs in-process."""
        return None

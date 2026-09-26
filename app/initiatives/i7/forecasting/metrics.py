"""Backtest metrics.

Pinball loss is primary; bias, fill rate and holding cost are comparison
metrics. Each returns a status alongside its value, because two of them cannot
be computed on the current data and reporting a zero would be a claim rather
than an absence.

Every formula is written out. A metric nobody can reproduce is a metric nobody
can challenge, and these decide whether a challenger model replaces a baseline.
"""

from decimal import Decimal

from app.initiatives.i7.forecasting.types import (
    BacktestMetrics,
    MetricStatus,
    OriginForecast,
)


def pinball_loss(paths: list[OriginForecast], quantile: float | None) -> tuple[Decimal | None, MetricStatus]:
    """Mean pinball loss at the target quantile.

    ::

        L = (1/n) * sum( q * (y - f)      where y >= f
                         (1 - q) * (f - y) where y <  f )

    Asymmetric by design: at a high quantile, under-forecasting is penalised
    far more than over-forecasting, which is what makes it the right objective
    for a service-level target.

    ``None`` quantile means the service-level matrix is unsigned. There is no
    neutral default -- 0.5 would silently turn this into mean absolute error and
    change which model wins.
    """
    if quantile is None:
        return None, MetricStatus.NOT_EVALUABLE
    if not paths:
        return None, MetricStatus.NOT_EVALUABLE

    q = Decimal(str(quantile))
    total = Decimal(0)
    for path in paths:
        error = path.actual - path.predicted
        total += q * error if error >= 0 else (Decimal(1) - q) * (-error)
    return total / Decimal(len(paths)), MetricStatus.AVAILABLE


def mean_error(paths: list[OriginForecast]) -> Decimal | None:
    """Mean of ``predicted - actual``.

    Signed on purpose: positive means the model over-forecasts. This is the
    quantity the adoption rule's "bias" refers to.
    """
    if not paths:
        return None
    total = sum((path.predicted - path.actual for path in paths), Decimal(0))
    return total / Decimal(len(paths))


def bias_percentage(paths: list[OriginForecast]) -> Decimal | None:
    """Mean error as a fraction of mean actual demand.

    Scale-free, so segments with different demand volumes compare directly.
    ``None`` when mean actual demand is zero -- a percentage of nothing has no
    meaning, and returning 0 would read as "unbiased".
    """
    if not paths:
        return None
    actual_total = sum((path.actual for path in paths), Decimal(0))
    if actual_total == 0:
        return None
    error_total = sum((path.predicted - path.actual for path in paths), Decimal(0))
    return error_total / actual_total


def mean_absolute_error(paths: list[OriginForecast]) -> Decimal | None:
    if not paths:
        return None
    total = sum((abs(path.predicted - path.actual) for path in paths), Decimal(0))
    return total / Decimal(len(paths))


def simulated_fill_rate(paths: list[OriginForecast]) -> tuple[Decimal | None, MetricStatus]:
    """Fraction of demand a forecast-sized stock position would have covered.

    ::

        fill_rate = sum(min(forecast, actual)) / sum(actual)

    The assumptions, stated because they matter: each period is treated
    independently, the forecast is taken as the quantity available, and nothing
    carries over between periods. This is a forecast-adequacy measure, not an
    inventory simulation -- a real one needs safety stock and a reorder point,
    which are Phase 5 and deliberately absent here.

    ``None`` when no demand occurred: a fill rate against zero demand is
    undefined, and 1.0 would flatter every dead material.
    """
    if not paths:
        return None, MetricStatus.NOT_EVALUABLE
    actual_total = sum((path.actual for path in paths), Decimal(0))
    if actual_total == 0:
        return None, MetricStatus.NOT_EVALUABLE
    covered = sum((min(path.predicted, path.actual) for path in paths), Decimal(0))
    return covered / actual_total, MetricStatus.AVAILABLE


def simulated_holding_cost(
    paths: list[OriginForecast], unit_price: Decimal | None, holding_rate: Decimal | None
) -> tuple[Decimal | None, MetricStatus]:
    """Cost of the stock a forecast implies holding.

    Needs a unit price and an annual holding rate. The rate comes from the Max
    Stock policy, which is unsigned, and it appears in no document and no seeded
    table -- so this is NOT_EVALUABLE on the current data. Inventing a rate would
    make one model look cheaper than another on a number nobody approved.
    """
    if unit_price is None or holding_rate is None:
        return None, MetricStatus.NOT_EVALUABLE
    if not paths:
        return None, MetricStatus.NOT_EVALUABLE

    excess = sum(
        (max(path.predicted - path.actual, Decimal(0)) for path in paths), Decimal(0)
    )
    monthly_rate = holding_rate / Decimal(12)
    return excess * unit_price * monthly_rate, MetricStatus.AVAILABLE


def evaluate(
    paths: list[OriginForecast],
    *,
    quantile: float | None,
    unit_price: Decimal | None = None,
    holding_rate: Decimal | None = None,
) -> BacktestMetrics:
    """Every metric for one model's rolling-origin paths."""
    loss, loss_status = pinball_loss(paths, quantile)
    fill, fill_status = simulated_fill_rate(paths)
    holding, holding_status = simulated_holding_cost(paths, unit_price, holding_rate)

    return BacktestMetrics(
        pinball_loss=loss,
        pinball_status=loss_status,
        mean_error=mean_error(paths),
        bias_percentage=bias_percentage(paths),
        fill_rate=fill,
        fill_rate_status=fill_status,
        holding_cost=holding,
        holding_cost_status=holding_status,
        mean_absolute_error=mean_absolute_error(paths),
    )

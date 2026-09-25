"""Read-only audit: how is SBA's alpha actually chosen, versus how the
Solution Design says it should be ("0.05 to 0.20, tune via grid search on
backtest")?

For each of the 3 requested materials, and for every candidate alpha, this
runs the SAME rolling-origin backtest the champion/challenger decision already
uses (``backtest.run``), scores each alpha by mean absolute error over its own
origins, and prints:

    alpha | origins | forecast/actual pairs | MAE | in-sample score (current code)

Nothing is written, and the production ``select_alpha`` is not modified by
this script -- it only calls the existing, unmodified functions to show what
each one currently produces.
"""

import sys
from decimal import Decimal

from app.core.db import get_sessionmaker
from app.initiatives.i7.forecasting import backtest as backtest_engine
from app.initiatives.i7.forecasting import metrics as metric_functions
from app.initiatives.i7.forecasting import sba
from app.initiatives.i7.forecasting.service import _load_candidates
from app.initiatives.i7.forecasting.types import ModelName

DEFAULT_MATERIALS = ("5000092261", "5000092262", "5000092269")
WIDTH = 100


def _table(headers, rows, indent="  "):
    cells = [[str(h) for h in headers]] + [[str(c) for c in row] for row in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    line = indent + "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    print(line)
    for n, row in enumerate(cells):
        print(indent + "|" + "|".join(f" {c:<{w}} " for c, w in zip(row, widths)) + "|")
        if n == 0:
            print(line)
    print(line)


def main(materials: list[str]) -> None:
    session_factory = get_sessionmaker()
    with session_factory() as session:
        candidates = {c.material: c for c in _load_candidates(session) if c.material in materials}
        session.rollback()

    print("=" * WIDTH)
    print("SBA ALPHA SELECTION AUDIT  (read-only)")
    print("=" * WIDTH)
    print(
        f"\nApproved grid: {[str(a) for a in sba.ALPHA_GRID]}   "
        f"(Solution Design: 'tune via grid search on backtest')"
    )
    print(f"Backtest: {backtest_engine.MINIMUM_TRAINING_MONTHS}-month minimum training, "
          f"{backtest_engine.REQUIRED_ORIGINS} required origins\n")

    for material in materials:
        candidate = candidates.get(material)
        print("\n" + "#" * WIDTH)
        print(f"#  MATERIAL {material}")
        print("#" * WIDTH)
        if candidate is None:
            print("  Not a routed SBA/LightGBM candidate (not INTERMITTENT/LUMPY with "
                  "sufficient history) -- nothing to audit.")
            continue

        series = candidate.series
        horizon = candidate.lead_time_months or 1
        print(f"\n  plant {candidate.plant}   series length {series.length} months   "
              f"horizon {horizon}   demand class {candidate.demand_class}")
        print(f"  monthly values: {[str(v) for v in series.values]}")

        rows = []
        current_alpha = sba.select_alpha(series.values)
        backtests_by_alpha = {}
        for alpha in sba.ALPHA_GRID:
            bt = backtest_engine.run(
                series,
                ModelName.SBA,
                "sba-audit",
                lambda s, h, a=alpha: sba.forecast(s, h, alpha=a),
                horizon,
            )
            backtests_by_alpha[alpha] = bt
            mae = metric_functions.mean_absolute_error(list(bt.paths)) if bt.paths else None
            in_sample = sba._in_sample_error(series.values, alpha)
            rows.append(
                [
                    alpha,
                    f"{bt.origins_evaluated}/{bt.available_origins}/{bt.required_origins}",
                    len(bt.paths),
                    f"{mae:.4f}" if mae is not None else "-",
                    f"{in_sample:.4f}" if in_sample is not None else "-",
                    "<- CURRENT" if alpha == current_alpha else "",
                ]
            )

        _table(
            ["alpha", "origins (eval/avail/req)", "forecast/actual pairs",
             "backtest MAE (score)", "in-sample |rate-mean| (current code)", ""],
            rows,
        )

        best_backtest = min(
            sba.ALPHA_GRID,
            key=lambda a: (
                metric_functions.mean_absolute_error(list(backtests_by_alpha[a].paths))
                or Decimal("Infinity")
            ),
        )
        current_forecast = sba.forecast(series, horizon)
        corrected_forecast = sba.forecast(series, horizon, alpha=best_backtest)

        print(f"\n  Current (in-sample) alpha:  {current_alpha}  ->  rate {current_forecast.rate}")
        print(f"  Correct (backtest) alpha:   {best_backtest}  ->  rate {corrected_forecast.rate}")
        print(
            "  Forecast changes: "
            + ("YES" if current_forecast.rate != corrected_forecast.rate else "NO")
        )

    print("\n" + "=" * WIDTH)
    print("Audit complete. Nothing was written; select_alpha() itself is unmodified.")
    print("=" * WIDTH)


if __name__ == "__main__":
    main(sys.argv[1:] or list(DEFAULT_MATERIALS))

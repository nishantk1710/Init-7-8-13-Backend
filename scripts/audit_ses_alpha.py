"""Read-only audit: how is SES's alpha actually chosen, versus how the
Formula Reference says it should be ("The parameter alpha is selected using
rolling-origin backtesting")?

For each of the 3 requested materials, and for every candidate alpha in SES's
own (0.05-0.95, 19-value) grid, this runs the SAME rolling-origin backtest the
champion/challenger decision already uses (``backtest.run``), scores each
alpha by mean absolute error over its own origins, and compares against the
current in-sample (one-step-ahead-over-the-whole-window) selection.

Nothing is written, and the production ``select_alpha`` is not modified by
this script.
"""

import sys
import time
from decimal import Decimal

from app.core.db import get_sessionmaker
from app.initiatives.i7.forecasting import backtest as backtest_engine
from app.initiatives.i7.forecasting import metrics as metric_functions
from app.initiatives.i7.forecasting import ses
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
    print("SES ALPHA SELECTION AUDIT  (read-only)")
    print("=" * WIDTH)
    print(f"\nGrid: {len(ses.ALPHA_GRID)} candidates, "
          f"{ses.ALPHA_GRID[0]} to {ses.ALPHA_GRID[-1]} in steps of 0.05")
    print("Formula Reference: 'The parameter alpha is selected using "
          "rolling-origin backtesting.'\n")

    for material in materials:
        candidate = candidates.get(material)
        print("\n" + "#" * WIDTH)
        print(f"#  MATERIAL {material}")
        print("#" * WIDTH)
        if candidate is None:
            print("  Not a routed SES/Auto-ARIMA candidate (not SMOOTH/ERRATIC with "
                  "sufficient history) -- nothing to audit.")
            continue

        series = candidate.series
        horizon = candidate.lead_time_months or 1
        print(f"\n  plant {candidate.plant}   series length {series.length} months   "
              f"horizon {horizon}   demand class {candidate.demand_class}")
        print(f"  monthly values: {[str(v) for v in series.values]}")

        rows = []
        current_alpha = ses.select_alpha(series.values)
        backtests_by_alpha = {}
        t0 = time.monotonic()
        for alpha in ses.ALPHA_GRID:
            bt = backtest_engine.run(
                series,
                ModelName.SES,
                "ses-audit",
                lambda s, h, a=alpha: ses.forecast(s, h, alpha=a),
                horizon,
            )
            backtests_by_alpha[alpha] = bt
            mae = metric_functions.mean_absolute_error(list(bt.paths)) if bt.paths else None
            in_sample = ses._in_sample_error(series.values, alpha)
            rows.append(
                [
                    alpha,
                    f"{bt.origins_evaluated}/{bt.available_origins}/{bt.required_origins}",
                    len(bt.paths),
                    f"{mae:.4f}" if mae is not None else "-",
                    f"{in_sample:.4f}",
                    "<- CURRENT" if alpha == current_alpha else "",
                ]
            )
        elapsed = time.monotonic() - t0

        _table(
            ["alpha", "origins (eval/avail/req)", "forecast/actual pairs",
             "backtest MAE (score)", "in-sample SSE-style error (current code)", ""],
            rows,
        )
        print(f"  (all {len(ses.ALPHA_GRID)} candidates backtested in {elapsed:.3f}s)")

        best_backtest = min(
            ses.ALPHA_GRID,
            key=lambda a: (
                metric_functions.mean_absolute_error(list(backtests_by_alpha[a].paths))
                if backtests_by_alpha[a].paths else Decimal("Infinity")
            ),
        )
        current_forecast = ses.forecast(series, horizon)
        corrected_forecast = ses.forecast(series, horizon, alpha=best_backtest)

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

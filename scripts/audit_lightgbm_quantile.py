"""Read-only audit: is LightGBM's service-level quantile applied per
material, or pooled across the whole matrix?

For each of the 3 requested materials, shows: criticality, the applicable
signed service level, the expected (this material's own) scoring quantile,
the quantile the OLD code actually used (max(levels), the pooled training
quantile applied to scoring too), the NEW code's scoring quantile (this
material's own, via ``_scoring_quantile``), and how each changes SBA's and
LightGBM's pinball loss / the champion decision.

Nothing is written; ``_scoring_quantile`` is called as the fixed production
code now defines it, not reimplemented here.
"""

import sys

from app.core.db import get_sessionmaker
from app.initiatives.i7.forecasting import lightgbm_model, sba
from app.initiatives.i7.forecasting import backtest as backtest_engine
from app.initiatives.i7.forecasting import metrics as metric_functions
from app.initiatives.i7.forecasting import selection
from app.initiatives.i7.forecasting.service import (
    _load_candidates,
    _scoring_quantile,
    _target_quantile,
)
from app.initiatives.i7.forecasting.types import ModelName
from app.initiatives.i7.features import BaselineModel
from app.initiatives.i7.policy.dev_fixtures import default_policy

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
    policy = default_policy()
    training_quantile = _target_quantile(policy)

    session_factory = get_sessionmaker()
    with session_factory() as session:
        all_candidates = _load_candidates(session)
        candidates = {c.material: c for c in all_candidates if c.material in materials}
        corpus = [c.series for c in all_candidates if c.baseline_model == BaselineModel.SBA.value]
        pooled, pooled_status, pooled_detail = lightgbm_model.train(corpus, training_quantile)
        session.rollback()

    print("=" * WIDTH)
    print("LIGHTGBM SERVICE-LEVEL QUANTILE AUDIT  (read-only)")
    print("=" * WIDTH)
    print(f"\nSigned service-level matrix: {list(policy.service_level.matrix)}")
    print(f"Pooled TRAINING quantile (max across signed tiers): {training_quantile}")
    print("(this is correct for training -- one pooled model can only be trained "
          "at one quantile; the bug is whether SCORING also used this instead of "
          "each material's own tier)\n")

    for material in materials:
        candidate = candidates.get(material)
        print("\n" + "#" * WIDTH)
        print(f"#  MATERIAL {material}")
        print("#" * WIDTH)
        if candidate is None:
            print("  Not a routed SBA/LightGBM candidate -- nothing to audit.")
            continue

        criticality = candidate.criticality
        try:
            own_level = policy.service_level.service_level_for(criticality) if criticality else None
        except Exception:
            own_level = None

        new_scoring_quantile = _scoring_quantile(policy, criticality)
        old_scoring_quantile = training_quantile  # what the buggy code used for scoring too

        horizon = candidate.lead_time_months or 1
        sba_bt = backtest_engine.run(
            candidate.series, ModelName.SBA, "sba-1", lambda s, h: sba.forecast(s, h), horizon
        )
        lgb_bt = backtest_engine.run(
            candidate.series,
            ModelName.LIGHTGBM,
            "lgbm-1",
            lambda s, h: lightgbm_model.forecast(s, h, pooled, pooled_status, pooled_detail),
            horizon,
        )

        old_sba_metrics = metric_functions.evaluate(list(sba_bt.paths), quantile=old_scoring_quantile)
        new_sba_metrics = metric_functions.evaluate(list(sba_bt.paths), quantile=new_scoring_quantile)
        old_lgb_metrics = metric_functions.evaluate(list(lgb_bt.paths), quantile=old_scoring_quantile)
        new_lgb_metrics = metric_functions.evaluate(list(lgb_bt.paths), quantile=new_scoring_quantile)

        sba_bt_old = sba_bt._replace(metrics=old_sba_metrics)
        sba_bt_new = sba_bt._replace(metrics=new_sba_metrics)
        lgb_bt_old = lgb_bt._replace(metrics=old_lgb_metrics)
        lgb_bt_new = lgb_bt._replace(metrics=new_lgb_metrics)

        old_decision = selection.decide_intermittent(f"{material}/{candidate.plant}", sba_bt_old, lgb_bt_old)
        new_decision = selection.decide_intermittent(f"{material}/{candidate.plant}", sba_bt_new, lgb_bt_new)

        _table(
            ["field", "value"],
            [
                ["Criticality", criticality.value if criticality else "(unresolved)"],
                ["Applicable signed service level", own_level if own_level is not None else "(none for this tier)"],
                ["Expected scoring quantile (this material's own)", new_scoring_quantile],
                ["Quantile OLD code used for scoring", old_scoring_quantile],
                ["Quantile NEW code uses for scoring", new_scoring_quantile],
                ["SBA pinball loss -- OLD quantile", old_sba_metrics.pinball_loss],
                ["SBA pinball loss -- NEW quantile", new_sba_metrics.pinball_loss],
                ["LightGBM pinball loss -- OLD quantile", old_lgb_metrics.pinball_loss],
                ["LightGBM pinball loss -- NEW quantile", new_lgb_metrics.pinball_loss],
                ["Champion/challenger -- OLD quantile", old_decision.adoption_status.value],
                ["Champion/challenger -- NEW quantile", new_decision.adoption_status.value],
                ["Decision CHANGES", "YES" if old_decision.adoption_status != new_decision.adoption_status else "NO"],
            ],
        )

    print("\n" + "=" * WIDTH)
    print("Audit complete. Nothing was written.")
    print("=" * WIDTH)


if __name__ == "__main__":
    main(sys.argv[1:] or list(DEFAULT_MATERIALS))

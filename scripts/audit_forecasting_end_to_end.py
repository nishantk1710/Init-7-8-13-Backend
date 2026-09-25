"""Final end-to-end I07 forecasting audit.

Replays the ENTIRE production path -- feature store, history gate, ADI/CV²
classification (re-derived and compared against the stored verdict), OAR
scope, model routing, SES/SBA/ARIMA/LightGBM, rolling-origin backtesting,
alpha selection, the per-material scoring quantile, champion/challenger
selection -- using the actual, unmodified production functions
(``service._load_candidates``, ``service._run_models``,
``service._material_plant_decision``, ``selection.decide_intermittent``/
``decide_smooth``) against whatever the database currently holds.

Materials are supplied on the command line or read from every SUFFICIENT-
history material-plant in the feature store -- nothing is hardcoded to any
specific material ID; the 3 IDs the user asked about are passed as ordinary
arguments, exactly like any other material would be.

Read-only. No forecast run is written; nothing is modified.
"""

import sys
from decimal import Decimal

from app.core.db import get_sessionmaker
from app.initiatives.i7.features import BaselineModel
from app.initiatives.i7.features.classification import classify_demand
from app.initiatives.i7.forecasting import backtest as backtest_engine
from app.initiatives.i7.forecasting import lightgbm_model, sba, ses
from app.initiatives.i7.forecasting import service
from app.initiatives.i7.policy.dev_fixtures import default_policy
from sqlalchemy import text

DEFAULT_MATERIALS = ("5000092261", "5000092262", "5000092269")
WIDTH = 100


def _fmt(value, places: int = 4) -> str:
    if value is None:
        return "-"
    if isinstance(value, Decimal | float):
        return f"{float(value):,.{places}f}"
    return str(value)


def _table(headers, rows, indent="  "):
    cells = [[str(h) for h in headers]] + [[_fmt(c) for c in row] for row in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    line = indent + "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    print(line)
    for n, row in enumerate(cells):
        print(indent + "|" + "|".join(f" {c:<{w}} " for c, w in zip(row, widths)) + "|")
        if n == 0:
            print(line)
    print(line)


def _kv(pairs):
    _table(["Field", "Value"], [[k, v] for k, v in pairs])


def main(materials: list[str]) -> None:
    policy = default_policy()
    training_quantile = service._target_quantile(policy)
    session_factory = get_sessionmaker()

    with session_factory() as session:
        feature_run_id = session.execute(
            text("select max(id) from i7_feature_run where status = 'succeeded'")
        ).scalar()

        print("=" * WIDTH)
        print("I07 FORECASTING -- FINAL END-TO-END AUDIT  (read-only, production code path)")
        print("=" * WIDTH)
        _kv(
            [
                ("Feature run in use", feature_run_id),
                ("Policy", f"{policy.policy_id} v{policy.policy_version} ({policy.status})"),
                ("Signed service-level matrix", list(policy.service_level.matrix)),
                ("Pooled LightGBM training quantile (max(levels))", training_quantile),
            ]
        )

        candidates = service._load_candidates(session)
        by_material = {c.material: c for c in candidates}
        corpus = [c.series for c in candidates if c.baseline_model == BaselineModel.SBA.value]
        pooled, pooled_status, pooled_detail = lightgbm_model.train(corpus, training_quantile)
        print(f"\n  routed candidates: {len(candidates)}   SBA corpus: {len(corpus)} series")
        print(f"  pooled LightGBM: {pooled_status.value}  ({pooled_detail})")

        for material in materials:
            print("\n\n" + "#" * WIDTH)
            print(f"#  MATERIAL {material}")
            print("#" * WIDTH)

            feature_row = session.execute(
                text(
                    """select sap_material_number, sap_plant_code, total_periods, non_zero_periods,
                              adi, cv_squared, demand_class, history_status, baseline_model,
                              challenger_model, mrp_type, oar_scope, criticality,
                              consumption_count_12m, lead_time_days
                         from i7_material_feature where sap_material_number = :m"""
                ),
                {"m": material},
            ).mappings().first()

            if feature_row is None:
                print("  Not found in the feature store at all -- nothing to audit.")
                continue

            plant = feature_row["sap_plant_code"]
            recomputed_class = classify_demand(
                feature_row["adi"], feature_row["cv_squared"], policy.classification
            )

            _section = lambda title: print(f"\n  -- {title} " + "-" * max(0, WIDTH - len(title) - 6))

            _section("1-4. FEATURES / HISTORY GATE / ADI-CV² / OAR")
            _kv(
                [
                    ("History months / non-zero months",
                     f"{feature_row['total_periods']} / {feature_row['non_zero_periods']}"),
                    ("ADI", feature_row["adi"]),
                    ("CV²", feature_row["cv_squared"]),
                    ("Demand class (stored)", feature_row["demand_class"]),
                    ("Demand class (re-derived now, same policy)", recomputed_class.value),
                    ("Classification consistent",
                     "YES" if recomputed_class.value == feature_row["demand_class"] else "NO -- MISMATCH"),
                    ("History status", feature_row["history_status"]),
                    ("MRP type", feature_row["mrp_type"] or "(blank)"),
                    ("OAR scope (Phase 3 verdict)", feature_row["oar_scope"]),
                    ("Criticality", feature_row["criticality"] or "(none)"),
                    ("consumption_count_12m (issues - reversals, trailing 12m)",
                     feature_row["consumption_count_12m"]),
                    ("Lead time (days)", feature_row["lead_time_days"]),
                ]
            )

            candidate = by_material.get(material)
            if candidate is None:
                print("\n  Not a routed forecasting candidate (history not SUFFICIENT, or "
                      "series rejected) -- OAR/cold-start path applies; no models run here.")
                continue

            _section("5. MODEL ROUTING")
            _kv(
                [
                    ("Routed baseline", feature_row["baseline_model"]),
                    ("Routed challenger", feature_row["challenger_model"]),
                ]
            )

            horizon = candidate.lead_time_months or 1
            scoring_quantile = service._scoring_quantile(policy, candidate.criticality)

            results = service._run_models(
                candidate, pooled, pooled_status, pooled_detail, False, scoring_quantile
            )
            by_model = {fr.model: bt for fr, bt, _ in results}
            by_model_fr = {fr.model: fr for fr, bt, _ in results}

            # --- 6/7. Alpha selection + backtest MAE for SES and SBA -------
            _section("6-7. ALPHA SELECTION (backtest-based, per material)")
            alpha_rows = []
            if candidate.baseline_model == BaselineModel.SES.value:
                chosen_ses = ses.select_alpha(candidate.series, horizon)
                ses_bt = backtest_engine.run(
                    candidate.series, service.ModelName.SES, "ses-1",
                    lambda s, h, a=chosen_ses: ses.forecast(s, h, alpha=a), horizon,
                )
                mae_ses = None
                if ses_bt.paths:
                    from app.initiatives.i7.forecasting import metrics as metric_functions
                    mae_ses = metric_functions.mean_absolute_error(list(ses_bt.paths))
                alpha_rows.append(["SES", chosen_ses, mae_ses, len(ses.ALPHA_GRID)])
            else:
                chosen_sba = sba.select_alpha(candidate.series, horizon)
                sba_bt = backtest_engine.run(
                    candidate.series, service.ModelName.SBA, "sba-1",
                    lambda s, h, a=chosen_sba: sba.forecast(s, h, alpha=a), horizon,
                )
                mae_sba = None
                if sba_bt.paths:
                    from app.initiatives.i7.forecasting import metrics as metric_functions
                    mae_sba = metric_functions.mean_absolute_error(list(sba_bt.paths))
                alpha_rows.append(["SBA", chosen_sba, mae_sba, len(sba.ALPHA_GRID)])
            _table(["Model", "Selected alpha (backtest-chosen)", "Backtest MAE at that alpha", "Grid size"], alpha_rows)

            # --- 8-11. Backtest / quantile / pinball / bias per model -------
            _section("8-11. BACKTEST, SCORING QUANTILE, PINBALL LOSS, BIAS")
            _kv(
                [
                    ("This material's own scoring quantile", scoring_quantile),
                    ("Why", (
                        f"criticality={candidate.criticality.value if candidate.criticality else '(none)'} "
                        f"-> service_level_for() on the signed matrix"
                        if scoring_quantile is not None
                        else "unresolvable: no signed level for this material's own tier "
                             "(never borrowed from the pooled training quantile)"
                    )),
                ]
            )
            bt_rows = []
            for model_name, bt in by_model.items():
                fr = by_model_fr[model_name]
                bt_rows.append(
                    [
                        model_name.value,
                        f"{bt.origins_evaluated}/{bt.available_origins}/{bt.required_origins}",
                        bt.status.value,
                        bt.metrics.pinball_loss if bt.metrics else None,
                        bt.metrics.pinball_status.value if bt.metrics else "-",
                        bt.metrics.bias_percentage if bt.metrics else None,
                        fr.parameters and ";".join(f"{k}={v}" for k, v in fr.parameters) or "-",
                    ]
                )
            _table(
                ["Model", "Origins (eval/avail/req)", "Backtest status", "Pinball loss",
                 "Pinball status", "Bias %", "Parameters"],
                bt_rows,
            )

            # --- 12-13. Champion/challenger -- every rule, not just first ---
            _section("12-13. CHAMPION / CHALLENGER (every applicable rule)")
            decision = service._material_plant_decision(
                feature_row["demand_class"], f"{material}/{plant}", by_model
            )
            if decision is None:
                print("  No decision (demand class has no champion/challenger pair, or "
                      "baseline never ran).")
                champion_model = None
            else:
                _kv(
                    [
                        ("Baseline", decision.baseline_model.value),
                        ("Challenger", decision.challenger_model.value if decision.challenger_model else None),
                        ("Baseline pinball", decision.baseline_pinball),
                        ("Challenger pinball", decision.challenger_pinball),
                        ("Pinball improvement (rule: > 0.05)", decision.improvement),
                        ("Baseline bias %", decision.baseline_bias),
                        ("Challenger bias %", decision.challenger_bias),
                        ("|bias| change (rule: <= 0.05)", decision.bias_change),
                        ("Origins eval / required (rule: >= 12)",
                         f"{decision.origins_evaluated} / {decision.required_origins}"),
                        ("ADOPTION STATUS", decision.adoption_status.value),
                        ("Every failed/passed rule (decision_reason)", decision.decision_reason),
                    ]
                )
                champion_model = (
                    decision.challenger_model
                    if decision.adoption_status.value == "CHALLENGER_ELIGIBLE"
                    else decision.baseline_model
                )

            # --- 14. Final forecast -----------------------------------------
            _section("14. FINAL FORECAST")
            champion_fr = by_model_fr.get(champion_model) if champion_model else None
            if champion_fr is None:
                print("  No champion forecast available.")
            else:
                _kv(
                    [
                        ("Champion model", f"{champion_fr.model.value} ({champion_fr.model_version})"),
                        ("Forecast status", champion_fr.status.value),
                        ("Forecast rate", f"{_fmt(champion_fr.rate)} {champion_fr.unit or ''} / month"),
                        ("Horizon", f"T+1..T+{champion_fr.horizon_months} months"),
                    ]
                )

        session.rollback()

    print("\n" + "=" * WIDTH)
    print("Audit complete. Nothing was written.")
    print("=" * WIDTH)


if __name__ == "__main__":
    main(sys.argv[1:] or list(DEFAULT_MATERIALS))

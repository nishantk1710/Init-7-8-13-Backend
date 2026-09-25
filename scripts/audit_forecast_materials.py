"""Read-only forecasting audit for a handful of material-plants.

Replays Phase 4 for the requested materials using the *unmodified* service
internals -- ``_load_candidates``, ``lightgbm_model.train``, ``_run_models`` and
``_material_plant_decision`` -- exactly as ``run_forecasting()`` calls them, and
prints the full decision trail per material:

    Features -> Demand classification -> Models run -> Backtest metrics
    -> Champion/challenger -> Selected model -> Final forecast

Nothing is written. Unlike ``run_forecast_evaluation.py`` no ``i7_forecast_run``
row is inserted; the session is rolled back. The pooled LightGBM is still
trained on the *whole* SBA corpus, because that is what production does --
training it on three series would audit a different model.

Usage (from the backend directory):
    ALLOW_NON_AZURE_SQL=1 .venv/Scripts/python.exe -m scripts.audit_forecast_materials 5000092261 5000092262
"""

import sys
from decimal import Decimal

from sqlalchemy import text

from app.core.db import get_sessionmaker
from app.initiatives.i7.features import BaselineModel
from app.initiatives.i7.features.classification import classify_demand
from app.initiatives.i7.forecasting import backtest as backtest_engine
from app.initiatives.i7.forecasting import lightgbm_model, selection
from app.initiatives.i7.forecasting import service
from app.initiatives.i7.policy.dev_fixtures import default_policy

DEFAULT_MATERIALS = ("5000092261", "5000092262", "5000092269")
WIDTH = 100


def _fmt(value, places: int = 4) -> str:
    if value is None:
        return "-"
    if isinstance(value, Decimal | float):
        return f"{float(value):,.{places}f}"
    return str(value)


def _table(headers: list[str], rows: list[list], indent: str = "  ") -> None:
    cells = [[str(h) for h in headers]] + [[_fmt(c) for c in row] for row in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    line = indent + "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    print(line)
    for n, row in enumerate(cells):
        print(indent + "|" + "|".join(f" {c:<{w}} " for c, w in zip(row, widths)) + "|")
        if n == 0:
            print(line)
    print(line)


def _kv(pairs: list[tuple[str, object]]) -> None:
    _table(["Parameter", "Value"], [[k, v] for k, v in pairs])


def _section(title: str) -> None:
    print(f"\n  -- {title} " + "-" * max(0, WIDTH - len(title) - 6))


def _feature_row(session, material: str):
    return session.execute(
        text(
            """
            SELECT * FROM i7_material_feature
             WHERE sap_material_number = :m
             ORDER BY sap_plant_code
            """
        ),
        {"m": material},
    ).mappings().all()


def _persisted(session, material: str, plant: str):
    run_id = session.execute(
        text("select max(id) from i7_forecast_run where status = 'succeeded'")
    ).scalar()
    if run_id is None:
        return None, []
    rows = session.execute(
        text(
            """
            SELECT model_name, is_champion, forecast_rate, pinball_loss, adoption_status
              FROM i7_forecast
             WHERE forecast_run_id = :r AND sap_material_number = :m AND sap_plant_code = :p
             ORDER BY model_name
            """
        ),
        {"r": run_id, "m": material, "p": plant},
    ).mappings().all()
    return run_id, rows


def main(materials: list[str]) -> None:
    policy = default_policy()
    quantile = service._target_quantile(policy)
    obsolescence_trigger_configured = False  # same constant as run_forecasting()

    session_factory = get_sessionmaker()
    with session_factory() as session:
        feature_run_id = session.execute(
            text("select max(id) from i7_feature_run where status = 'succeeded'")
        ).scalar()

        print("=" * WIDTH)
        print("I07 FORECASTING AUDIT  (read-only -- no forecast run is written)")
        print("=" * WIDTH)
        _kv(
            [
                ("Policy", f"{policy.policy_id} v{policy.policy_version} ({policy.status})"),
                ("Feature run in use", feature_run_id),
                ("Target quantile (service level)", quantile),
                ("ADI cutoff", policy.classification.adi_cutoff),
                ("CV² cutoff", policy.classification.cv_squared_cutoff),
                ("History gate: min months / min non-zero",
                 f"{policy.history_gate.minimum_history_months} / "
                 f"{policy.history_gate.minimum_non_zero_periods}"),
                ("Backtest: required origins", backtest_engine.REQUIRED_ORIGINS),
                ("Backtest: min training months", backtest_engine.MINIMUM_TRAINING_MONTHS),
                ("Adoption: min pinball improvement (selection.py)",
                 f"> {selection.MINIMUM_PINBALL_IMPROVEMENT}"),
                ("Adoption: max bias deterioration (selection.py)",
                 f"<= {selection.MAXIMUM_BIAS_DETERIORATION}"),
                ("TSB obsolescence trigger configured", obsolescence_trigger_configured),
            ]
        )

        print("\nLoading every routed material-plant (needed to train the pooled "
              "LightGBM on the same corpus production uses)...")
        candidates = service._load_candidates(session)
        corpus = [c.series for c in candidates if c.baseline_model == BaselineModel.SBA.value]
        pooled, pooled_status, pooled_detail = lightgbm_model.train(corpus, quantile)
        print(f"  routed candidates: {len(candidates)}   SBA corpus: {len(corpus)} series")
        print(f"  pooled LightGBM:   {pooled_status.value}  ({pooled_detail})")

        by_key = {(c.material, c.plant): c for c in candidates}

        for material in materials:
            print("\n\n" + "#" * WIDTH)
            print(f"#  MATERIAL {material}")
            print("#" * WIDTH)

            features = _feature_row(session, material)
            if not features:
                print("  Not in i7_material_feature -- nothing to audit.")
                continue

            for f in features:
                plant = f["sap_plant_code"]
                print(f"\n  >>> {material} / plant {plant}")

                # 1. Features ------------------------------------------------
                _section("1. INPUT FEATURES (i7_material_feature, feature_run "
                         f"{f['feature_run_id']})")
                _kv(
                    [
                        ("History window", f"{f['first_period']} .. {f['last_period']}"),
                        ("Total periods / non-zero periods",
                         f"{f['total_periods']} / {f['non_zero_periods']}"),
                        ("Consumption events (12m)", f["consumption_count_12m"]),
                        ("Total demand", f["total_demand"]),
                        ("Mean demand (all periods)", f["mean_demand_all_periods"]),
                        ("Std dev (all periods)", f["std_dev_demand_all_periods"]),
                        ("Mean non-zero demand", f["mean_non_zero_demand"]),
                        ("Std dev non-zero demand", f["std_dev_non_zero_demand"]),
                        ("History status", f["history_status"]),
                        ("Data sufficiency", f["data_sufficiency"]),
                        ("Lead time (days) / source",
                         f"{_fmt(f['lead_time_days'], 1)} / {f['lead_time_source']}"),
                        ("MRP type / material status", f"{f['mrp_type']} / {f['material_status']}"),
                        ("Criticality", f["criticality"]),
                        ("OAR scope", f["oar_scope"]),
                    ]
                )

                candidate = by_key.get((material, plant))
                if candidate is not None:
                    s = candidate.series
                    print(f"\n  Monthly demand series ({s.length} months, unit={s.unit}):")
                    _table(
                        ["Period"] + [p.period.strftime("%y-%m") for p in s.points],
                        [["Qty"] + [_fmt(p.quantity, 0) for p in s.points]],
                    )

                # 2. Classification -----------------------------------------
                _section("2. DEMAND CLASSIFICATION (Syntetos-Boylan)")
                recomputed = classify_demand(f["adi"], f["cv_squared"], policy.classification)
                adi_side = ("<=" if f["adi"] is not None and
                            f["adi"] <= Decimal(str(policy.classification.adi_cutoff)) else ">")
                cv_side = ("<=" if f["cv_squared"] is not None and
                           f["cv_squared"] <= Decimal(str(policy.classification.cv_squared_cutoff))
                           else ">")
                _kv(
                    [
                        ("ADI", f"{_fmt(f['adi'])}  ({adi_side} {policy.classification.adi_cutoff})"
                                f"  [{f['adi_status']}]"),
                        ("CV²", f"{_fmt(f['cv_squared'])}  ({cv_side} "
                                f"{policy.classification.cv_squared_cutoff})  [{f['cv_squared_status']}]"),
                        ("Demand class (stored)", f["demand_class"]),
                        ("Demand class (re-derived now)", recomputed.value),
                        ("Consistent", "YES" if recomputed.value == f["demand_class"] else "NO -- MISMATCH"),
                        ("Routing", f"baseline={f['baseline_model']}  challenger={f['challenger_model']}"),
                        ("Routing reason", f["routing_reason"]),
                    ]
                )

                if candidate is None:
                    print("\n  Not routed to forecasting (history gate / class / series "
                          "rejected) -- no models run. This is the OAR/cold-start path.")
                    continue

                # 3 + 4. Models and backtests --------------------------------
                results = service._run_models(
                    candidate, pooled, pooled_status, pooled_detail,
                    obsolescence_trigger_configured, quantile,
                )
                horizon = candidate.lead_time_months

                _section(f"3. MODELS RUN  (horizon T+1..T+{horizon} months, "
                         f"from {_fmt(f['lead_time_days'], 1)} days / 30.44)")
                _table(
                    ["Model", "Role", "Version", "Fit status", "Rate/month", "Training window",
                     "Parameters"],
                    [
                        [
                            fr.model.value,
                            "baseline" if is_base else "challenger",
                            fr.model_version,
                            fr.status.value,
                            fr.rate,
                            f"{fr.training_start} .. {fr.training_end}" if fr.training_start else "-",
                            ", ".join(f"{k}={v}" for k, v in fr.parameters) or "-",
                        ]
                        for fr, _, is_base in results
                    ],
                )
                for fr, bt, _ in results:
                    if fr.detail:
                        print(f"    note {fr.model.value}: {fr.detail}")

                _section("4. BACKTESTING (rolling origin)")
                _table(
                    ["Model", "Backtest status", "Origins eval/avail/req", "Paths",
                     "Pinball", "MAE", "Mean error", "Bias %", "Fill rate", "Holding cost"],
                    [
                        [
                            bt.model.value,
                            bt.status.value,
                            f"{bt.origins_evaluated}/{bt.available_origins}/{bt.required_origins}",
                            len(bt.paths),
                            bt.metrics.pinball_loss if bt.metrics else None,
                            bt.metrics.mean_absolute_error if bt.metrics else None,
                            bt.metrics.mean_error if bt.metrics else None,
                            bt.metrics.bias_percentage if bt.metrics else None,
                            bt.metrics.fill_rate if bt.metrics else None,
                            (bt.metrics.holding_cost if bt.metrics and bt.metrics.holding_cost
                             is not None else (bt.metrics.holding_cost_status.value
                                               if bt.metrics else None)),
                        ]
                        for _, bt, _ in results
                    ],
                )
                for _, bt, _ in results:
                    if bt.detail:
                        print(f"    note {bt.model.value}: {bt.detail}")

                # 5. Champion / challenger ----------------------------------
                by_model = {fr.model: bt for fr, bt, _ in results}
                decision = service._material_plant_decision(
                    candidate.demand_class, f"{material}/{plant}", by_model
                )
                _section("5. CHAMPION / CHALLENGER")
                if decision is None:
                    print("  No decision (demand class has no champion/challenger pair).")
                    champion_model = None
                else:
                    _kv(
                        [
                            ("Baseline", decision.baseline_model.value),
                            ("Challenger", decision.challenger_model.value
                             if decision.challenger_model else None),
                            ("Baseline pinball", decision.baseline_pinball),
                            ("Challenger pinball", decision.challenger_pinball),
                            ("Pinball improvement (need > 0.05)", decision.improvement),
                            ("Baseline bias %", decision.baseline_bias),
                            ("Challenger bias %", decision.challenger_bias),
                            ("|bias| change (need <= 0.05)", decision.bias_change),
                            ("Origins eval / required",
                             f"{decision.origins_evaluated} / {decision.required_origins}"),
                            ("Adoption status", decision.adoption_status.value),
                            ("Reason", decision.decision_reason),
                        ]
                    )
                    champion_model = (
                        decision.challenger_model
                        if decision.adoption_status.value == "CHALLENGER_ELIGIBLE"
                        else decision.baseline_model
                    )

                # 6 + 7. Selected model and final forecast ------------------
                _section("6. SELECTED MODEL  /  7. FINAL FORECAST")
                champion = next(
                    ((fr, bt) for fr, bt, _ in results if fr.model == champion_model), None
                )
                if champion is None:
                    print("  No champion.")
                else:
                    fr, bt = champion
                    why = (
                        "challenger cleared all three adoption bars"
                        if champion_model == (decision.challenger_model if decision else None)
                        else f"baseline retained -- {decision.adoption_status.value}"
                    )
                    _kv(
                        [
                            ("Selected model", f"{fr.model.value} ({fr.model_version})"),
                            ("Why", why),
                            ("Forecast status", fr.status.value),
                            ("Forecast rate", f"{_fmt(fr.rate)} {fr.unit or ''} / month"),
                            ("Horizon", f"T+1..T+{fr.horizon_months} months"),
                            ("Implied demand over horizon (rate x horizon)",
                             (fr.rate * fr.horizon_months) if fr.rate is not None
                             and fr.horizon_months else None),
                            ("Backtest evidence", f"{bt.status.value}, "
                                                  f"{bt.origins_evaluated}/{bt.required_origins} origins"),
                        ]
                    )

                # Cross-check with the last persisted run ---------------------
                run_id, stored = _persisted(session, material, plant)
                if stored:
                    _section(f"CROSS-CHECK vs persisted forecast run {run_id}")
                    live = {fr.model.value: fr for fr, _, _ in results}
                    live_bt = {fr.model.value: bt for fr, bt, _ in results}
                    _table(
                        ["Model", "Stored champion", "Stored rate", "Audit rate",
                         "Stored pinball", "Audit pinball", "Match"],
                        [
                            [
                                r["model_name"], r["is_champion"], r["forecast_rate"],
                                live[r["model_name"]].rate if r["model_name"] in live else None,
                                r["pinball_loss"],
                                (live_bt[r["model_name"]].metrics.pinball_loss
                                 if r["model_name"] in live_bt and live_bt[r["model_name"]].metrics
                                 else None),
                                "YES" if r["model_name"] in live and _fmt(r["forecast_rate"], 6)
                                == _fmt(live[r["model_name"]].rate, 6) else "NO",
                            ]
                            for r in stored
                        ],
                    )

        session.rollback()

    print("\n" + "=" * WIDTH)
    print("Audit complete. Nothing was written to the database.")
    print("=" * WIDTH)


if __name__ == "__main__":
    main(sys.argv[1:] or list(DEFAULT_MATERIALS))

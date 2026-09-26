"""Fresh Phase 4 forecasting/model-evaluation run against the SAP data
currently staged/featured in PostgreSQL.

Calls the existing, unmodified app.initiatives.i7.forecasting.service.run_forecasting()
-- every backtest, metric, and selection rule below is Phase 4 code exactly as
implemented; this script only reports what that call writes and returns. It
inserts one new i7_forecast_run row (forecasting has no idempotency
short-circuit in this codebase -- unlike staging/features/inventory/OAR, it
always recomputes). No formula, threshold, or business rule is changed here.
"""
from app.core.db import get_sessionmaker
from app.initiatives.i7.forecasting.service import run_forecasting
from sqlalchemy import text


def main():
    sf = get_sessionmaker()

    with sf() as session:
        feature_run_id = session.execute(
            text("select max(id) from i7_feature_run where status='succeeded'")
        ).scalar()
        n_materials = session.execute(
            text("select count(*) from i7_material_feature")
        ).scalar()
        history_counts = dict(session.execute(
            text("select history_status, count(*) from i7_material_feature group by 1")
        ).all())

    print("=" * 80)
    print("PHASE 4 -- FRESH FORECASTING / MODEL EVALUATION RUN")
    print("=" * 80)
    print(f"\nFeature generation in use: feature_run_id={feature_run_id}")
    print(f"Material-plants in feature store: {n_materials}")
    print(f"History status breakdown: {history_counts}")
    print("\nRunning models (SES/Auto-ARIMA, SBA/LightGBM, TSB)... this recomputes "
          "every backtest fresh, it does not reuse a prior forecast run.\n")

    result = run_forecasting()

    print("-" * 80)
    print(f"RUN RESULT  (forecast_run_id={result.run_id}, status={result.status})")
    print("-" * 80)
    print(f"Materials evaluated:  {result.materials_evaluated}")
    print(f"Forecast rows written: {result.forecasts_written}")
    print(f"Model status counts:    {result.model_status_counts}")
    print(f"Backtest status counts: {result.backtest_status_counts}")
    if result.error:
        print(f"ERROR: {result.error}")
        return

    # --- Per-material-plant, per-model detail: exactly what was persisted ---
    with sf() as session:
        rows = session.execute(
            text("""
                select sap_material_number, sap_plant_code, demand_class, model_name,
                       is_baseline, forecast_status, forecast_rate, forecast_unit,
                       training_start, training_end, horizon_months, parameters,
                       backtest_status, required_origins, available_origins,
                       origins_evaluated, pinball_loss, pinball_status, mean_error,
                       bias_percentage, mean_absolute_error, fill_rate,
                       fill_rate_status, detail
                from i7_forecast where forecast_run_id = :rid
                order by sap_material_number, sap_plant_code, model_name
            """), {"rid": result.run_id}
        ).mappings().all()

    print(f"\n{'=' * 80}\nPER MATERIAL-PLANT / PER MODEL DETAIL  ({len(rows)} rows)\n{'=' * 80}")
    for r in rows:
        print(f"\n[{r['sap_material_number']} / {r['sap_plant_code']}]  "
              f"demand_class={r['demand_class']}  model={r['model_name']} "
              f"(baseline={r['is_baseline']})")
        print(f"  forecast_status: {r['forecast_status']}")
        if r['forecast_status'] != 'MISSING_REQUIRED_FEATURE':
            print(f"  training window: {r['training_start']} .. {r['training_end']} "
                  f"({r['horizon_months']}-month horizon)")
            print(f"  parameters: {r['parameters'] or '(none)'}")
            print(f"  forecast_rate: {r['forecast_rate']} {r['forecast_unit'] or ''}")
        print(f"  backtest_status: {r['backtest_status']}  "
              f"(origins: {r['origins_evaluated']} evaluated / "
              f"{r['available_origins']} available / {r['required_origins']} required)")
        if r['origins_evaluated'] and r['origins_evaluated'] < r['required_origins']:
            print(f"  *** BELOW REQUIRED ORIGINS -- reported as PARTIAL/insufficient, "
                  f"NOT treated as production-ready. Requirement not lowered. ***")
        print(f"  pinball_loss: {r['pinball_loss']}  (status: {r['pinball_status']})")
        print(f"  mean_absolute_error (MAE): {r['mean_absolute_error']}")
        print(f"  RMSE: NOT IMPLEMENTED in this codebase (metrics.py has no rmse function) -- "
              f"not reported, not invented")
        print(f"  bias (mean_error): {r['mean_error']}   bias_percentage: {r['bias_percentage']}")
        print(f"  fill_rate: {r['fill_rate']}  (status: {r['fill_rate_status']})")
        if r['detail']:
            print(f"  detail: {r['detail']}")

    # --- Segment-level champion/challenger selection decisions ---
    print(f"\n{'=' * 80}\nSEGMENT MODEL-SELECTION DECISIONS\n{'=' * 80}")
    for d in result.segment_decisions:
        print(f"\nSegment: {d.segment_key}")
        print(f"  baseline: {d.baseline_model.value}  pinball={d.baseline_pinball}  "
              f"bias={d.baseline_bias}")
        print(f"  challenger: {d.challenger_model.value if d.challenger_model else None}  "
              f"pinball={d.challenger_pinball}  bias={d.challenger_bias}")
        print(f"  improvement: {d.improvement}   bias_change: {d.bias_change}")
        print(f"  origins: {d.origins_evaluated} evaluated / {d.required_origins} required")
        print(f"  DECISION: {d.adoption_status.value}")
        print(f"  reason: {d.decision_reason}")


if __name__ == "__main__":
    main()

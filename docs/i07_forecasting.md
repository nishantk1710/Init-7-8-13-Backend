# I07 Forecasting (Phase 4)

Demand forecasting and rolling-origin backtesting over the Phase 3 feature store.

```
Feature Store
     ↓
History Gate  (Phase 3 is the authority — never recomputed here)
     ↓
Demand Class
     ↓
┌───────────────────────────────┐   ┌───────────────────────────────┐
│ Smooth / Erratic              │   │ Intermittent / Lumpy          │
│ SES baseline                  │   │ SBA baseline                  │
│ Auto-ARIMA challenger         │   │ LightGBM challenger           │
└───────────────────────────────┘   └───────────────────────────────┘
     ↓
High Obsolescence Risk → TSB candidate
     ↓
Rolling-origin backtest (T+1 … T+LT)
     ↓
Segment champion/challenger decision
     ↓
Demand forecast (units per month)
```

**Models predict demand only.** Nothing here produces safety stock, a reorder
point or a maximum — those stay deterministic Phase 5 calculations over the
forecast, which is what keeps them auditable. A test asserts no inventory column
exists on the forecast table.

## Running it

```bash
.venv\Scripts\python.exe -c "from app.initiatives.i7.forecasting import run_forecasting; print(run_forecasting())"
```

~200s for 471 material-plants × 3 models, backtested over up to 10 origins each.

## Structure

| Module | Responsibility |
| --- | --- |
| `types.py` | statuses, model names, result contracts |
| `series.py` | validated demand series, training windows |
| `ses.py` | SES baseline |
| `arima.py` | Auto-ARIMA challenger |
| `sba.py` | SBA baseline |
| `lightgbm_model.py` | global pooled quantile challenger |
| `tsb.py` | obsolescence-aware candidate |
| `metrics.py` | pinball loss, bias, fill rate, holding cost |
| `backtest.py` | rolling-origin engine |
| `selection.py` | champion/challenger, per segment |
| `service.py` | orchestration and persistence |

## The models

**SES** — `l_t = α·y_t + (1−α)·l_{t−1}`, forecast `= l_t`. Written directly
rather than via `statsmodels.SimpleExpSmoothing`: the recurrence is three lines,
matches the Formula Reference line for line, and the library optimises α by
maximum likelihood whereas the source requires backtest selection. α is chosen
from a 0.05–0.95 grid by one-step-ahead error over the training window.

**Auto-ARIMA** — order search over 12 candidate `(p,d,q)` combinations scored by
AIC, fitted with `statsmodels`. Not `pmdarima`: it is effectively unmaintained
and does not build on Python 3.14, and its `auto_arima` is itself a loop over
statsmodels fits. Negative predictions are floored at zero — ARIMA is unbounded
and demand is not.

**SBA** — `p_t = α·q_t + (1−α)·p_{t−1}`, `z_t = α·d_t + (1−α)·z_{t−1}`, forecast
`= (1 − α/2)·z/p`. The `(1 − α/2)` factor is Croston's bias correction and is
what makes this SBA. Updates happen **only on demand events**; zero months
lengthen `q_t`, which is how the interval grows for a material going quiet. α is
tuned within the documented 0.05–0.20 range.

**TSB** — `p` decays on every empty period, so a material approaching
end-of-life sees its forecast fall. SBA cannot do this: its interval only
updates when demand arrives, so it never learns from silence.

**LightGBM** — one global pooled quantile model, never one per material. A
material here holds at most 13 observations.

## Rolling-origin protocol

Train through T → forecast T+1…T+LT → score against actuals → advance T by one
month → repeat. The horizon is the material's mean observed lead time from Phase
2 staging, converted to whole months.

**Leakage is structural.** A model at origin *i* receives `series.through(i)` and
nothing else — there is no code path by which a later observation reaches it. A
test records every training window the harness passes and asserts none contained
a future value. LightGBM features are built from `values[:i]` with the target at
`i`, and a test asserts `months_observed == target_index`, proving the window
stopped short of its own target.

## Metrics

| Metric | Status here |
| --- | --- |
| Pinball loss (primary) | **NOT_EVALUABLE** — needs the target quantile |
| Bias | Available — mean(predicted − actual) / mean(actual), signed |
| Fill rate | Available — `Σ min(forecast, actual) / Σ actual` |
| Holding cost | **NOT_EVALUABLE** — no holding rate exists |

Fill-rate assumptions, stated because they matter: each period is independent,
the forecast is treated as the quantity available, nothing carries over. It is a
forecast-adequacy measure, not an inventory simulation — a real one needs safety
stock and a reorder point, which are Phase 5.

## Champion/challenger

**Intermittent/Lumpy (SBA vs LightGBM)** — the documented rule: pinball
improvement > 5% **and** bias not worsening by > 5%, over ≥ 12 origins. All three
must hold. Bias is compared on magnitude, so −5% → +20% counts as deterioration.

**Smooth/Erratic (SES vs Auto-ARIMA)** — the documents require the comparison but
**state no numeric threshold**, so none is invented. Metrics and relative
improvement are computed and reported; the verdict stays `BASELINE_RETAINED`
pending a signed criterion.

Decisions are made **per segment**, never per SKU, and metrics are pooled across
the segment's material-plants before comparison. Origin depth is summarised as
the deepest series in the segment, not the sum: summing would report 1,756
origins for a segment of 250 materials that each have 7, and clear a bar none of
them meets.

## Three statuses, deliberately separate

```
model_status     = SUCCESS                            (the fit ran)
backtest_status  = PARTIAL_DEVELOPMENT_DATA           (10 of 12 origins)
adoption_status  = NOT_ELIGIBLE_INSUFFICIENT_ORIGINS  (not production-ready)
```

Collapsing these would force that case to be reported as either a failure or an
adoption, and it is neither.

## Results on the July/August extract

471 material-plants forecast, 1,413 forecast rows per run.

| Model | SUCCESS | Blocked | Origins (avg) |
| --- | --- | --- | --- |
| SES | 90 | 9 no lead time | 9.6 |
| Auto-ARIMA | 90 | 9 no lead time | 6.6 |
| SBA | 360 | 12 no lead time | 6.9 |
| LightGBM | 0 | 360 service level unset | — |
| TSB | 0 | 450 trigger unset | — |

**Segment decisions** — all four blocked, honestly:

| Segment | Baseline / Challenger | Origins | Status |
| --- | --- | --- | --- |
| SMOOTH | SES / Auto-ARIMA | 10/12 | NOT_ELIGIBLE_INSUFFICIENT_ORIGINS |
| ERRATIC | SES / Auto-ARIMA | 10/12 | NOT_ELIGIBLE_INSUFFICIENT_ORIGINS |
| INTERMITTENT | SBA / LightGBM | 10/12 | NOT_EVALUABLE (service level) |
| LUMPY | SBA / LightGBM | 10/12 | NOT_EVALUABLE (service level) |

**No challenger is adopted anywhere.** That is the correct outcome on this data.

## Limitations

**~13-month history → at most 10 origins.** The required 12 is unreachable:
13 months minus a 3-month minimum training window leaves 10, before any horizon.
The requirement is **unchanged** — rolling-origin, ≥12 origins, pinball primary,
adoption only on the documented criteria.

> The current extract does not provide enough historical depth to produce the
> required ≥12 rolling origins for all applicable series. Phase 4 distinguishes
> development validation from production acceptance evidence and does not lower
> the acceptance threshold.

**Service-level matrix unsigned → LightGBM cannot be evaluated.** It is a
quantile model and the quantile *is* the service level. No default: 0.95 and
0.98 give materially different forecasts and neither is attributable to a
business decision. The quantile is read from policy, so a signed matrix switches
this on with no code change.

**Obsolescence trigger unset → TSB is not a candidate.** `MSTAE = '01'` is *not*
the trigger — it marks a material already flagged obsolete, while the documented
trigger identifies materials *approaching* obsolescence. The Formula Reference
requires the trigger, initialisation and parameter range to be validated on
Vedanta history.

**Lead time missing for 21 material-plants** → `NOT_EVALUABLE_LEAD_TIME_UNAVAILABLE`.
No 30/60/90-day substitute; the fallback policy is unresolved.

**Holding cost not evaluable** — no holding rate exists in any document or table.

**LightGBM feature set is `demand-only-1`.** The Solution Design's
material-attribute and lead-time feature groups are **omitted, not imputed**:
criticality reaches 1.7% of material-plants and lead-time statistics are Phase 5
policy. The feature version is recorded so a later run on fuller data is
distinguishable.

## Model versioning

Deterministic implementation identifiers — `ses-1`, `sba-1`,
`arima-statsmodels-1`, `lgbm-quantile-1`, `tsb-1` — incremented when behaviour
changes. Not semantic versions: nothing here maintains a release contract, and a
fabricated `v1.3` would imply a history that does not exist.

## Persistence

`i7_forecast_run` (one per execution, with policy and quantile) ·
`i7_forecast` (every model's result per material-plant, not just the champion) ·
`i7_segment_decision` (the champion/challenger audit log).

Origin-by-origin paths are **not** persisted — roughly 450 × 4 × 10 rows per run
that nothing downstream reads. They exist in memory during a run, which is where
the metrics come from.

Runs are append-only: each is an immutable record keyed by `forecast_run_id`, so
history is preserved for audit rather than overwritten.

## What Phase 5 reads

`i7_forecast`, filtered to `forecast_status = 'SUCCESS'` and the baseline model
for each demand class — the demand rate in units per month, plus its
`training_end` and `horizon_months`. Safety stock, ROP and maximum are computed
there, not here.

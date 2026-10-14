# I07 declining demand and the separate TSB path

Status: **built, off by default, not validated on Vedanta history.** Nothing here
runs unless `I7_DECLINING_DEMAND_MODE` is changed, and the table it needs is added
by a migration that is applied on purpose.

## What it does

A rule (v1) finds material-plants whose demand has been falling. A flagged
material-plant is forecast with **TSB directly** — no ARIMA, LightGBM or SBA
challenger, no backtest, no comparison. Everything else keeps the normal routing.
The inventory and recommendation stages are unchanged except for two small
things described under *Downstream* below.

A flag is a statement about demand. It does **not** mean the material is
obsolete, and it never changes OAR routing or obsolete-material handling
(`MSTAE = '01'` still gets no safety stock, whatever the forecast).

## The rule

12 consecutive complete months, split into an earlier half (months 1–6) and a
recent half (months 7–12). Flagged `DECLINING_DEMAND` only when **all** hold,
checked in this order (the first one failed is the stored reason):

| # | Condition | Status if it fails |
|---|---|---|
| 1 | 12 complete months available | `INSUFFICIENT_HISTORY` |
| 2 | at least **4** of the 12 months have demand | `INSUFFICIENT_HISTORY` |
| 3 | at least **3** of the earlier 6 months have demand | `INSUFFICIENT_EARLY_DEMAND` |
| 4 | `decline = 1 − recent_rate / early_rate` is at least **0.50** | `DECLINE_THRESHOLD_NOT_MET` |
| 5 | the latest **3** months are all zero | `RECENT_DEMAND_PRESENT` |

`rate` = months with demand in the half ÷ 6. A month has demand when its quantity
is above zero. Gaps in the months, an unknown quantity or no months at all give
`DATA_NOT_EVALUABLE`. The thresholds are `DecliningDemandPolicy` in
`policy/thresholds.py` (part of the policy document), and `rule_version`
(`declining-v1`) is stored with every result.

Example `10,0,8,0,6,0 | 0,5,0,0,0,0`: 4 months with demand, 3 in the earlier half
(3/6), 1 in the recent half (1/6), decline 66.7 %, latest 3 months empty →
`DECLINING_DEMAND`.

### What "missing" means

Inside the extract's observation window a month with no consumption movement **is
zero demand**: staging stores only months that had movements, and ADI, the history
gate and forecasting all densify the gaps to explicit zeros. "Missing" means
months **outside** that window — history nobody delivered. Those are never
invented as zeros: fewer than 12 complete months is `INSUFFICIENT_HISTORY`.

### The latest month is treated as incomplete

Nothing in the extract says whether its last month was fully delivered, and staging
records no data cut-off date. So the latest staged month is **dropped** and the
window ends one month earlier. With a 12-month window the extract therefore needs
**at least 13 months** of coverage; the feature build logs a warning when it has
fewer, and every evaluation then says `INSUFFICIENT_HISTORY` ("11 complete
months…"). Recording the last posting date at staging would let a complete last
month count — a possible later change, deliberately not made now.

Only material-plants with enough history for the normal gate are evaluated; the
rest (cold start, no history) go to OAR and are not part of this.

## Where each part lives

| Part | Where |
|---|---|
| The rule (pure function) | `app/initiatives/i7/features/declining.py` |
| Thresholds | `DecliningDemandPolicy` in `app/initiatives/i7/policy/thresholds.py` |
| Evaluation, written by the feature build | `features/builder.py` → table `i7_declining_demand_evaluation` |
| TSB path and routing | `forecasting/service.py` (`_run_tsb_path`) |
| Switch | `I7_DECLINING_DEMAND_MODE` (`app/core/config.py`) |

The rule is implemented once and evaluated in the feature build — the stage that
already decides demand class and routing. Forecasting reads the stored result; it
never recomputes it.

## The three modes

| Mode | Feature build | Forecasting |
|---|---|---|
| `off` (default) | nothing evaluated | exactly as before, **including** the placeholder TSB row per material-plant |
| `shadow` | evaluates and stores; logs counts and reasons | routing unchanged; **no TSB rows at all** (the placeholders stop) |
| `active` | as shadow | flagged → TSB directly; everything else unchanged; no placeholders |

`shadow` and `off` never read the evaluation table, so they work on a database
that has not had the migration yet.

**Active refuses to run on missing evaluations.** If any forecasting candidate has
no evaluation for the run's feature run, the forecast run fails with a clear
message ("…have no evaluation for feature run N. Rebuild the features with
I7_DECLINING_DEMAND_MODE=shadow or active first…") and forecasts nothing. Zero
flagged is a normal answer and does not fail. A run is not resumed across a
change of mode.

## What is stored for a flagged material-plant

* A **TSB** row in `i7_forecast`: `is_champion = true`, `forecast_status SUCCESS`
  when TSB produced a forecast, `adoption_status TSB_DECLINING_DEMAND`,
  `backtest_status NOT_RUN_DECLINING_DEMAND`. `parameters` starts with
  `declining_rule=…;evaluated_months=YYYY-MM..YYYY-MM;` then TSB's alpha, beta,
  `p_final`, `z_final`.
* The **baseline** forecast (SES or SBA, the model the demand class would have
  used) as a separate row, `is_baseline = true`, `is_champion = false`, for
  review only. It is not compared with TSB.
* One row in `i7_segment_decision` (`TSB_DECLINING_DEMAND`, every metric empty).

TSB's own requirements still apply. A flagged material-plant whose TSB does not
produce a forecast has **no champion** and no fallback to the baseline (no policy
defines one); the inventory stage reports it as not evaluable. By construction a
flagged material-plant always meets TSB's minimum (2 observations, at least one
with demand), so this is a guard and not an expected case.

## Downstream

* **Inventory and recommendations read `is_champion AND forecast_status =
  'SUCCESS'`**, so a TSB champion flows through with no change to how they pick the
  forecast.
* **Intermittent and lumpy safety stock** takes `p_final` from the champion as the
  smoothed *interval* between demands (SBA's). TSB's `p_final` is a *probability*
  (0–1). Inventory therefore converts a TSB row — interval = 1 / p — before the
  compound-Poisson formula and the Monte Carlo simulation, which makes the event
  rate lead-time × p, TSB's own occurrence rate. A probability of zero is "not
  evaluable". **This conversion is a modelling choice that needs sign-off.**
  Smooth and erratic safety stock uses the history's variability and is
  unaffected; the lower TSB rate lowers the reorder point through lead-time demand.
* **The recommendation text** for a TSB forecast says demand has been falling,
  in plain words, in place of "projected with a method designed for…".

## The existing TSB model — unchanged

`tsb.py` is **not modified**. For the record:

* `p` starts as the share of months with demand and `z` as the mean non-zero size,
  both computed over the **whole training window**; the recurrence then runs over
  **the same window**. The early months are therefore counted twice (once in the
  start values, once in the recurrence). The design document leaves the exact
  convention to be validated on Vedanta history, so this was left as it is.
* `alpha = beta = 0.10`, not tuned; no search is performed.
* Minimum 2 observations and at least one month with demand.
* Training uses the full series, including the latest staged month, while the
  declining rule's window excludes it.

## The migration (not applied)

`alembic/versions/d3f8a1c47b92_i7_declining_demand_evaluation.py`, revision after
`e5b8a3c61d20`. It creates one table, `i7_declining_demand_evaluation`
(`feature_run_id, material, plant`, status, reason, rule version, window dates,
month counts, rates, decline percentage, `latest_months_zero`, `evaluated_at`),
a unique key on `(feature_run_id, sap_material_number, sap_plant_code)` and two
indexes. Nothing existing is touched.

* **Applying it:** deliberately on purpose — `python -m alembic upgrade head` from
  the SSH console. `startup.sh` runs migrations only when
  `RUN_MIGRATIONS_ON_STARTUP=true`; **check that setting before merging**, because
  with it on, the next start-up applies the migration.
* **Rollback:** `python -m alembic downgrade -1` drops the table and its two
  indexes. Nothing else references it, and the code reads it only in `active` mode.
* **Order:** the migration can be applied before or after the code is deployed.

## Enabling it later

1. Merge and deploy with the mode `off` (the default). Behaviour is unchanged.
2. Review and apply the migration.
3. Set `I7_DECLINING_DEMAND_MODE=shadow` and run the **features** stage. Read the
   log line `declining-demand (shadow, rule declining-v1): N evaluated, M flagged,
   by status {...}` and check the extract covers 13+ months.
4. **Have the business review the flagged list** (the table has each flagged
   material-plant with its counts, rates and window) before anything uses it.
   Passing tests do not validate the rule.
5. Set `I7_DECLINING_DEMAND_MODE=active`, run features, then forecasting. The
   forecasting log reports how many were flagged, how many TSB forecasts were
   made and how many were not evaluable.
6. The nightly trigger is a separate switch and is not touched by any of this.

## Known limits

* The thresholds are a proposal, not validated on Vedanta history; the extract has
  about 13 months, so any window is weakly supported.
* No backtest is run for TSB: the rule is the only gate, so a wrong rule changes
  numbers with nothing to catch it. The baseline row and the business review of the
  flagged list are what stand in for the check.
* The rule is class-agnostic: a smooth material with three empty latest months and
  enough earlier demand could be flagged. Worth reading in shadow mode.
* Placeholder TSB rows from earlier runs stay in the database. Nothing deletes
  them; a cleanup, if wanted, should be a separate reviewed change.
* The "Declining" option in the recommendations Type filter is **not** built: it
  needs the evaluation table deployed and the API to expose the flag.

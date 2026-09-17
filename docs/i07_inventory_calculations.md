# I07 Inventory Calculations (Phase 5)

Converts the Phase 4 demand forecast into stocking parameters.

```
Demand Forecast (Phase 4)
       ↓
Lead-Time Analysis
       ↓
Service-Level Policy  →  Z
       ↓
Safety Stock
       ↓
ROP
       ↓
Max Stock
```

> **ML and statistical models predict demand. These formulas calculate SS, ROP
> and Max.**

That separation is the Solution Design's, and it is what keeps the stocking
numbers auditable: every one can be re-derived by hand from the trace stored
beside it. No model output is ever a stocking parameter.

## Running it

```bash
.venv\Scripts\python.exe -c "from app.initiatives.i7.inventory import run_inventory_calculations; print(run_inventory_calculations())"
```

~6s for all 45,409 material-plants.

## Structure

| Module | Responsibility |
| --- | --- |
| `lead_time.py` | PO durations, exclusions, fallback tiers, σ_LT |
| `variability.py` | D_avg and σ_D over all periods, zeros included |
| `service_level.py` | signed matrix → service level → Z |
| `safety_stock.py` | Path A (normal) and Path B (compound Poisson) |
| `monte_carlo.py` | simulated lead-time demand for LUMPY |
| `rop.py` | E[LTD] + SS |
| `max_stock.py` | pluggable strategies behind a policy gate |
| `service.py` | orchestration and persistence |

## Lead time

`LT_i = GR date − PO creation date`, excluding cancelled POs and durations below
1 day, flagging those above 730 days as outliers. Then the mean and the **sample**
standard deviation (`m − 1`), converted at **30.44 days/month** — the documented
factor; 30 or 31 would shift every lead time and so every safety stock.

| POs | Method | Status |
| --- | --- | --- |
| ≥ 5 | `ACTUAL_STATISTICAL` | SUCCESS |
| 2–4 | `ACTUAL_WARNING` | WARNING |
| 0–1 | `PLANNED_FALLBACK` — SAP PLIFZ, σ_LT = 0.3 × planned | LIMITED |
| 0–1, no PLIFZ | — | `NOT_EVALUABLE_LEAD_TIME` |

No constant is ever substituted: the lead-time fallback policy beyond PLIFZ is
unresolved business work.

## Demand variability

`σ_D` over **all** periods including zeros — the Formula Reference says so
outright ("Include zeros — they represent real zero-demand months"). Deliberately
not σ_nz, which excludes them and feeds CV² instead. Phase 3 stores both.

## Service level — the business gate

`Z = Φ⁻¹(service_level)` via `scipy.stats.norm.ppf`. The arithmetic is trivial;
the gate is not. Solution Design Rule 1:

> THIS MUST COME FROM VEDANTA, NOT FROM CODE.
> Do NOT invent percentages. Do NOT assume 98% for critical.
> If policy is unsigned → system blocks recommendations.

So with an unsigned matrix the result is `NOT_EVALUABLE_SERVICE_LEVEL_UNSET`,
Z is null, and everything downstream blocks. An invented service level does not
fail loudly — it produces a safety stock that looks reasonable, cites a Z nobody
approved, and gets signed off.

Percentages appearing in tests are mathematical fixtures verifying `norm.ppf`,
never configuration.

## Safety stock

**Path A — SMOOTH / ERRATIC**

```
SS = Z × √( LT_avg × σ_D²  +  D_avg² × σ_LT² )
```

**Path B — INTERMITTENT / LUMPY**

```
λ        = LT_avg / p_final
E[d²]    = σ_nz² + μ_nz²
Var[LTD] = λ × E[d²]
SS       = Z × √Var[LTD]
```

The normal formula does not work for intermittent demand: a series that is mostly
zeros has a mean and variance describing nothing that actually happens. The
compound-Poisson form separates *how often* demand arrives from *how large* it is.

**LUMPY — Monte Carlo.** 10,000 simulations: draw `N ~ Poisson(λ)`, draw `N`
sizes from the **historical non-zero** distribution, sum; then
`SS = Quantile(service_level, LTD) − E[LTD]`. Preferred for lumpy demand because
its lead-time demand has a long right tail and the service-level quantile is read
from exactly that tail, where a normal approximation is least trustworthy.

Sizes come only from observed non-zero demand — never zeros, forecasts, or a
fitted distribution.

**Deterministic**: the seed is a SHA-256 of material, plant and formula version.
Python's built-in `hash` is randomised per process and would break
reproducibility invisibly; a timestamp seed would make a recommendation
irreproducible the moment anyone asked how it was reached.

## ROP

```
E[LTD] = forecast_rate × LT_avg_months
ROP    = E[LTD] + SS
```

The **forecast rate**, not the historical average — substituting `D_avg` would
quietly discard the forecasting layer and make the champion/challenger work
pointless.

## Max Stock — unsigned

All three candidate strategies are implemented; none is selected.

| Strategy | Formula | Blocker |
| --- | --- | --- |
| EOQ | `√(2·D_annual·S / (H·P))`, `Max = SS + EOQ` | S and H exist in no document or table |
| Review-period | `Max = ROP + (D_rate × T)` | T unsupplied |
| Hybrid | — | undefined until a policy defines it |

With no signed strategy the engine returns `NOT_CONFIGURED` and a null maximum.
**There is no `2 × ROP` fallback anywhere** — the Solution Design prohibits it by
name, and a test asserts its absence. An unrecognised strategy name also yields
not-configured rather than falling back to a working formula.

## Rounding

Safety stock, ROP and Max round **UP** to whole units, once, at the end.
Intermediate values stay exact and are kept in the trace — rounding a term would
compound through a square root.

## Invalid results

A negative quantity is a `CALCULATION_ERROR`, surfaced rather than clamped.
`max(value, 0)` would hide the cause, and the cause is always wrong input.

## Statuses

`SUCCESS` · `WARNING` · `LIMITED` · `NOT_EVALUABLE_NO_HISTORY` ·
`NOT_EVALUABLE_LEAD_TIME` · `NOT_EVALUABLE_SERVICE_LEVEL_UNSET` ·
`NOT_EVALUABLE_INVALID_FORECAST` · `NOT_EVALUABLE_INSUFFICIENT_DEMAND` ·
`NOT_EVALUABLE_COST_DATA` · `NOT_APPLICABLE_OBSOLETE` · `NOT_CONFIGURED` ·
`DEFERRED_TO_OAR` · `CALCULATION_ERROR`

Per output, not per calculation: lead time can succeed while safety stock blocks
on an unsigned service level, which is the state of 100% of the catalogue today.

## OAR deferral

NO_HISTORY and COLD_START materials carry `DEFERRED_TO_OAR`. Nothing here invents
a neighbour, picks a similar material, or substitutes a global average — the
similarity engine is Phase 6.

## Obsolete materials

A material whose criticality tier is OBSOLETE gets `NOT_APPLICABLE_OBSOLETE`,
never a zero-valued recommendation. Lead time and variability are still recorded:
the data is useful even where no recommendation follows. No obsolescence trigger
is invented — `MSTAE = '01'` is not treated as one.

## Formula validation

Every worked example in the Formula Reference reproduces:

| Example | Document | Computed |
| --- | --- | --- |
| Z table (85→99.5%) | 1.04 … 2.58 | all match to 0.01 |
| Lead time (6 POs) | 121.5 d, σ 7.50 d, 3.99 mo | exact |
| Path A | term₁ 7.49, term₂ 2.06, SS 6.33 → **7** | exact |
| Path B | λ 2.375, E[d²] 33.459, Var 79.47, SS 18.27 → **19** | exact |
| ROP smooth | E[LTD] 23.26, ROP 30.26 → **31** | exact |
| ROP intermittent | E[LTD] 12.69, ROP 31.69 → **32** | exact |
| EOQ | EOQ 9.46 → Max **17** | exact |
| Review-period | 48.49 → **49** | exact |

## Results on the current configuration

45,409 calculations. **No safety stock, ROP or maximum is produced** — by design.

| Output | Status | Count |
| --- | --- | --- |
| Lead time | SUCCESS (≥5 POs) | 250 |
| | WARNING (2–4 POs) | 164 |
| | LIMITED (PLIFZ fallback) | 55 |
| | NOT_EVALUABLE | 44,940 |
| Service level | NOT_EVALUABLE_SERVICE_LEVEL_UNSET | 45,409 |
| Safety stock | DEFERRED_TO_OAR | 44,938 |
| | NOT_EVALUABLE_SERVICE_LEVEL_UNSET | 469 |
| | NOT_EVALUABLE_LEAD_TIME | 2 |
| Max stock | NOT_CONFIGURED | 471 |

Exclusions: 1,130 cancelled POs, 8 durations under a day, 0 outliers above 730
days.

**The pipeline is proven to work.** With a test-fixture service level the same
code produces, for example, `4000002744/1300` SMOOTH: Z 1.645, SS 5 (raw 4.829),
ROP 10 (raw 9.677) — and Max Stock still `NOT_CONFIGURED`, because that gate is
separate.

## Idempotency

A run is identified by `(feature_run, forecast_run, policy_id, policy_version,
formula_version)` under a unique constraint. Repeating with unchanged inputs
reuses the existing run rather than writing a second copy; changing any input
legitimately creates a new one, so history is preserved for audit.

## Blockers

**Service-level matrix unsigned** — blocks all safety stock, and therefore all
ROP. The single highest-value unblock.
**Max Stock strategy unsigned** — and neither candidate is computable anyway: no
ordering cost, no holding rate, no review period.
**Lead time** — 2 forecastable material-plants have neither PO history nor PLIFZ.
**Criticality 1.7%** — it is the service-level matrix key, so even a signed matrix
would reach few materials until MARA coverage improves.
**Currency/cost absent** — blocks EOQ independently of the strategy decision.
**44,938 material-plants deferred to Phase 6.**

# I07 Feature Store (Phase 3)

Turns Phase 2 staging into demand features, a demand class, a model route and an
OAR verdict — one row per material-plant.

```
i7_staged_*  ──→  feature builder  ──→  i7_material_feature  ──→  Phase 4
```

Reads staging only, never `raw_*`. Computes features and decisions — no
forecasts, no inventory parameters, no champion.

## Running it

```bash
.venv\Scripts\python.exe -c "from app.initiatives.i7.features import build_features; print(build_features())"
```

~60s for 45,409 material-plants. Idempotent.

## Structure

| Module | Responsibility |
| --- | --- |
| `features/statistics.py` | ADI, CV², dispersion — pure functions over a series |
| `features/classification.py` | history gate, Syntetos-Boylan matrix, model routing |
| `features/oar_scope.py` | OAR verdict + reason at material-plant grain |
| `features/builder.py` | orchestration and persistence |

Every threshold comes from the Phase 1 `PolicyDocument`. No cutoff is written in
these modules — a literal `1.32` would make recalibration a code change.

## Decisions worth knowing

**Two dispersion measures, kept separate.** The Formula Reference uses different
populations: CV² over non-zero observations only, σ_D over *all* periods
including zeros ("Include zeros — they represent real zero-demand months").
Both are stored. σ_D is a Phase 5 input and is not used here.

**Unavailable is NULL plus a status, never 0.** ADI is undefined when n_nz = 0;
CV² needs two non-zero observations before a sample standard deviation exists.
Both are common — 41,187 material-plants have no non-zero demand at all. Storing
0 would classify them as SMOOTH, the class carrying the *least* safety stock, on
the strength of no evidence.

**Classification only runs behind the gate.** A cold-start material never gets a
demand class; ADI and CV² over three observations do not mean anything, and a
label would give the OAR path a classification it never earned.

**Densification spans the extract's window, not each material's own.** Staging
holds only months with movements; the builder fills the whole observation window
(2025-08 → 2026-08, 13 months) with explicit zeros. The Formula Reference defines
`n` as "total periods (months)" and gates on `total_history_months` — both
properties of the window, not of where a material happens to have moved.

Densifying per-material understates ADI: a material whose first movement is late
loses its leading zero-demand months from `n` while keeping every `n_nz`. Those
months are evidence of *no demand*, which is exactly what an intermittency
measure needs. Nothing outside the window is invented.

**Routing names models; it does not select one.** No champion column exists —
that needs Phase 4 backtesting.

## Validated against the documents

The Formula Reference worked example (§1.4–1.6) reproduces exactly:

| | Document | Computed |
| --- | --- | --- |
| n / n_nz | 24 / 14 | 24 / 14 |
| ADI | 1.71 | 1.71 |
| μ_nz | 5.57 | 5.57 |
| σ_nz | 1.56 | 1.55 (document rounds) |
| CV² | 0.078 | 0.078 |
| Class | INTERMITTENT | INTERMITTENT |

Cutoff semantics are `<=`, tested on both sides at 1e-6 resolution: ADI 1.32 is
frequent, 1.320001 is sporadic; CV² 0.49 is stable, 0.490001 is erratic.

## Results on the July/August extract

**History status**

| | Count |
| --- | --- |
| NO_HISTORY | 41,140 |
| COLD_START | 3,798 |
| SUFFICIENT | 471 |

Exclusive and exhaustive: the three sum to 45,409. With a fixed 13-month window
the `< 6 months` limb can no longer fire for a material that has any history, so
COLD_START is driven purely by `n_nz < 5`.

**Demand class** — UNCLASSIFIED 44,938 · INTERMITTENT 259 · LUMPY 113 ·
SMOOTH 75 · ERRATIC 24

**Routing** — SBA/LightGBM 372 · SES/Auto-ARIMA 99 · unrouted 44,938

**OAR scope** — UNKNOWN 44,374 · OUT_OF_SCOPE 1,035 · **IN_SCOPE 0**

**Attribute availability**

| Attribute | Coverage |
| --- | --- |
| MRP type (DISMM) | 45,360 (99.9%) |
| Consumption | 4,269 (9.4%) |
| Lead time | 2,779 (6.1%) |
| Criticality | 788 (1.7%) |
| **Material status (MSTAE)** | **72 (0.2%)** |

## Limitations and blockers

**B4 (blocking) — OAR identification yields zero IN_SCOPE.** The rule is
`DISMM ∈ {ND,PD} AND MSTAE ≠ '01'`. DISMM is available on 99.9% of rows, but
**MSTAE is available on 0.2%**. The AND therefore evaluates to UNKNOWN for
44,374 material-plants (97.7%).

Root-caused during the Phase 3 verification pass — **this is the source extract,
not an extraction defect**:

| Step | Finding |
| --- | --- |
| `raw_mara.x_plant_matl_status` | column present; only values are `01` and blank |
| Populated in raw MARA | 82 of 5,904 rows (1.4%) |
| Preserved by Phase 2 staging | 82 of 82 — nothing lost |
| Reaching the feature store | 72 (the other 10 materials have no MARC row) |

`82 − 10 = 72` accounts for every row. The extraction is correct; the extract
simply does not carry the field.

> Fuller MARA/MSTAE source coverage is required before reliable OAR
> identification can run on this development extract.

The same MARA coverage limitation causes the criticality gap (B1).

This is the policy behaving correctly: a missing MSTAE is *unknown*, not "not
obsolete", and treating it as the latter would assert something the data does not
say. But it means OAR identification cannot run on this extract. 44,394 rows
carry DISMM ∈ {ND,PD} and would be candidates if MSTAE were known.

Three possible resolutions, all business decisions — **not resolved here**:
1. Obtain a fuller MARA extract (also fixes the criticality gap).
2. Rule that a missing MSTAE means "not obsolete" — changes the predicate's
   `unknown_values`, a one-line config edit, but it is an assertion about data
   the business must make.
3. Drop MSTAE from the rule, reverting to the Solution Design's DISMM-only form.

**B1 (Phase 2, unchanged) — criticality reaches 1.7%.** Same root cause: it is
staged on the material record sourced from MARA.

**History is 13 months.** 471 material-plants pass the gate; none reaches the 24
months confidence HIGH requires, so `data_sufficiency` is LIMITED for the 4,269
with any history and INSUFFICIENT for the 41,140 without. Recorded, not worked
around — thresholds are unchanged and no history is fabricated.

**Backtesting cannot meet its bar on this extract.** The acceptance requirement
is unchanged and must stay so: rolling-origin backtest, ≥12 origins, pinball loss
as primary metric, with fill rate, bias and holding cost alongside, and challenger
adoption only on the documented improvement criteria.

> The current extract does not provide enough historical depth to produce the
> required ≥12 rolling origins for all applicable series. Phase 4 must
> distinguish development validation from production acceptance evidence and
> must not silently lower the acceptance threshold.

**Still unresolved, untouched:** movement-type set, OAR business confirmation,
OAR roll-up (`NOT_CONFIGURED` on every row), service levels, Max Stock strategy,
lead-time ownership, criticality source, currency source.

## What Phase 4 reads

`i7_material_feature`, filtered by `demand_class` and `baseline_model` — indexed
for exactly that. 471 material-plants are classified and routed; the other 44,938
are UNCLASSIFIED and belong to the OAR cold-start path (Phase 6).

# I07 safety stock: deactivated, not removed

Safety stock (SS) is no longer calculated or recommended by I07, for stock items and for OAR. **Nothing was
deleted.** The calculation code, the formulas, the SAP source data (`current_safety_stock`), the stored historical
recommendations and the database columns are all still there. Turning a switch back on restores the previous
behaviour for that category.

## The switches

Two settings, read once per run into `PolicyDocument.safety_stock` (`app/initiatives/i7/policy/thresholds.py`,
`SafetyStockPolicy`), through `policy.dev_fixtures.default_policy()`:

| Setting (env var) | Default | Category |
|---|---|---|
| `i7_stock_item_safety_stock_enabled` (`I7_STOCK_ITEM_SAFETY_STOCK_ENABLED`) | `false` | stock items (non-OAR) |
| `i7_oar_safety_stock_enabled` (`I7_OAR_SAFETY_STOCK_ENABLED`) | `false` | OAR |

They are independent. The model default of `SafetyStockPolicy` is **on** (the previous behaviour), so a bare
`PolicyDocument()` and every existing direct caller are unchanged; only `default_policy()` (used by every pipeline
entry point) applies the deployed default.

## What changes while a switch is off

**Stock items**

```
ROP = ceil(forecast_rate x LT)
Max = ceil(forecast_rate x (LT + T))      # review-period strategy, from the UNROUNDED lead-time demand
```

* No SS is calculated; the inventory row's `safety_stock_status` is `NOT_APPLICABLE_SAFETY_STOCK_DISABLED` and
  `safety_stock` is `NULL` (never 0).
* Still blocking: no or non-positive lead time, no or negative forecast, Obsolete, too little history, the
  missing-data window. The lead-time check moved from the SS step into the ROP step.
* No longer blocking: SS / Z-factor / service-level / variability / SBA-parameter failures.
* `READY_FOR_REVIEW` needs the ROP only (`recommendations/builder.py`); it reads the row's own status, not the setting.
* Smooth/Erratic SAP overrides still apply to ROP and Max. SAP's SS is not copied into the recommendation (it stays
  on the material as `current_safety_stock`).
* `Max` rounding: `rop.py` returns `raw_rop`; `max_stock.py` adds the review-period demand to it, so
  `ceil(10.2 x (1.1 + 1)) = 22`, not `ceil(12 + 10.2) = 23`. `ROP <= Max` holds by construction
  (`ceil(x) <= ceil(x + y)`) for T > 0 and rate, LT >= 0; each is enforced.
* The EOQ strategy is unchanged. It needs `safety_stock`, so with SS off it returns `NOT_EVALUABLE_COST_DATA`
  ("EOQ requires safety_stock") rather than a wrong number. It is not selected anywhere in the repo.

**OAR**

```
ROP = ceil(weighted_median(ROP_i, s_i))
Max = max( ceil(weighted_median(Max_i, s_i)), ROP )
SS  = not estimated (None); the donors' SS is not aggregated
```

* One place decides it, `oar.service.summarise_neighbours`, so a run and a "similar material removed" recalculation
  agree. Scoring, weights and the weighted median are untouched.
* **Donor rule:** a donor needs a current SAP ROP and max stock above zero (unchanged). Its safety stock is no longer
  a condition -- the `AND f.current_safety_stock > 0` line was removed from `oar/repository.py` (`_DONOR_WHERE`), for
  every setting. A donor with no SAP safety stock lends `None` (never 0). To bring SS back as a recommendation, put
  that line back **and** switch `I7_OAR_SAFETY_STOCK_ENABLED` on; otherwise donors with no SS would pull the SS median down.
  The OAR blend (off by default) follows the same rule: a peer is usable on its ROP and max stock; one with no SS
  adds nothing to the blended SS and changes neither the blended ROP nor Max.
* The recommendation carries no SS even if the stored OAR run does; the blend's Max never used SS, and its text no
  longer says a safety stock "is kept".

**Both:** Adjust takes `new_rop` and `new_max_stock` (`new_safety_stock` optional). Without it `ROP <= Max` is
checked and the recommended SS is left exactly as it is, never set to 0. A manual Min-Max conversion request needs
ROP and Max only. Adoption already skips a field with no approved value, so a row with no SS adopts on ROP and Max.
AI explanations use the `v3` prompts, which do not mention safety stock.

## Run identity: a run is never reused under the other setting

| Stage | Key component | SS on | SS off |
|---|---|---|---|
| Inventory | `InventoryRun.formula_version` | `i07-formula-2` | `i07-formula-3` |
| OAR | `OarRun.algorithm_version` | `oar-similarity-12` | `oar-similarity-12+no-ss` |
| Recommendations (per row) | `Recommendation.formula_version` | `i07-recommendation-8` | `...+stock-no-ss` / `...+oar-no-ss` |

Per stage and per category, so flipping one switch does not re-key the other category. The Monte Carlo seed still
derives from `FORMULA_VERSION`, so re-enabling SS reproduces the same draws. Removing a similar material from an
existing OAR recommendation keeps working whatever setting its run was made under: it is recalculated from the run's
stored neighbours under the current setting (ROP and Max, no SS), and the replaced values are kept as a snapshot.
The Similar Materials list no longer shows safety stock.

**Existing rows are never rewritten or deleted.** A recommendation in the approval chain or approved and waiting
for SAP is held back by the generator and keeps its ROP, Max, SS, explanation and history. New runs write new rows.
There is no migration and no backfill: status columns are free strings and the SS columns are nullable.

## How to reactivate

1. Set `I7_STOCK_ITEM_SAFETY_STOCK_ENABLED=true` and/or `I7_OAR_SAFETY_STOCK_ENABLED=true`.
2. Run the pipeline. The inventory / OAR / recommendation keys differ from the "off" ones, so fresh rows are written.
3. The frontend follows the data: a non-null recommended SS shows again (comparison box, Approvals line, Adjust
   field, table columns). No UI flag.
4. The AI prompts (`v3`) still omit SS. Add a `v4` pair if explanations should mention it again.

## Reporting dependencies (decision B is pending: nothing here was changed)

Current SAP SS and historical recommended SS are preserved. A missing new recommendation is never read as 0 (these
all exclude NULL): `net_safety_stock_value_impact`, the quarterly Safety Stock section, the Safety Stock baseline
row, the SS mean in stock levels, the SS export columns. Unaffected: Working Capital, Value Change and Excess
(ROP and Max), and the safety-stock *value* KPI (it uses current SAP SS).

Options for the report presentation (to decide): leave as is (the figures cover only rows that still carry a
recommended SS, i.e. none from new runs); label the SS rows "historical / not recommended"; or hide them for new
periods. Stored KPI snapshots and quarterly report JSON keep their old figures until recaptured or regenerated.
Also check `i7_kpi_formula_version.document_json` for a saved formula that uses `recommended_safety_stock`: it
would silently drop every row that has none.

## Open decisions

* **A. Erratic ROP above Max.** A SAP ROP kept next to a calculated Max can exceed it (SAP 850, Max 166). Nothing is
  changed: the figures and status stay, the row's `max_stock_trace` carries `rop_exceeds_max=...`, and the run logs
  a warning with the count. Options: leave; set Max to not-evaluable with a reason; or use SAP's Max/ROP together.
* **B.** Report presentation (above).
* **C. Decided: T stays 1 month, as the default.** `policy.dev_fixtures.default_max_stock_policy()` gives every
  tier (CRITICAL, IMPACT, INSURANCE, NORMAL; OBSOLETE never reaches Max) a review period of 1 month, so
  `default_policy()` uses the review-period strategy with no dev flag: `Max = ceil(rate x (LT + 1))`. It is a code
  constant (`DEFAULT_REVIEW_PERIOD_MONTHS`) so a change goes through a normal code change. `I7_DEV_MOCK_MAX_STOCK`
  still loads the same values from the YAML for a developer who wants to try other periods. A bare `PolicyDocument()`
  is unchanged (no strategy). The policy document itself is still DRAFT: this is a default, not a signed policy.

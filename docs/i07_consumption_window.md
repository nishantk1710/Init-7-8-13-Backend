# I07 demand window: 24 calendar months, ending at the latest verified complete month

## What changed

Before, every I07 demand statistic was computed over the extract's own first-to-last month (155 months on the July
2026 extract), and its last month counted whether or not SAP had delivered it in full. On that extract June, July
and August hold 339, 13 and 1 movements against about 2,400 in May: each has rows, none is complete, and all three
were being read as months in which nothing was consumed.

Now the window is `window_months` (24) calendar months ending at the latest month whose SAP consumption is
**verified complete**. It applies to classification (ADI / CV-squared), forecasting and the stock figures for every
material-plant outside OAR scope. OAR is untouched (see below).

## When a month is complete

`app/initiatives/i7/features/consumption_window.py` is the one place that decides. A month is **not** complete
because it has rows. Each month is judged against the median movement volume of the (up to 6) complete months
before it:

| Verdict | When | Effect |
| --- | --- | --- |
| `COMPLETE` | volume is at least 25% of the reference, or the operator validated it | counts; a material-plant with no movement in it consumed **zero** |
| `MISSING_DATA` | no rows, or under 25% of the reference | never zero, never the end of the window |

A flagged month's volume is not added to the reference, so one bad month cannot lower the bar for the next. The
window ends on the latest complete month; later staged months are recorded as `window_trailing_months` and are not
read. The first 3 months of a delivery have nothing before them and are accepted on holding movements.

All of it is `ConsumptionWindowPolicy` in the policy document: `window_months`, `completeness_reference_months`,
`completeness_min_reference_months`, `completeness_min_ratio`.

## What happens to MISSING_DATA

* **Months after the window's end** (the current situation: June to August 2026): excluded, listed on the run. The
  window stays usable, but the figures are only as current as the last complete month.
* **A month inside the window** (an interior gap): the window is `MISSING_DATA`. Every material-plant outside OAR
  scope gets `window_history_status = 'MISSING_DATA'` and `demand_class = UNCLASSIFIED`, so it is not forecast, its
  stock figures are `NOT_EVALUABLE`, and its recommendation is `NOT_EVALUABLE` with a `MISSING_DATA` blocking reason.
  The workflow only submits `READY_FOR_REVIEW`, so it cannot enter approval. Nothing is filled with zeros.
* **How to clear it:** reload the extract and re-run the pipeline, **or** check the month against SAP and list it in
  `I7_DEMAND_VALIDATED_MONTHS` (`YYYY-MM`, comma-separated), then re-run the features.

Because completeness is judged per month across the whole extract, one missing month blocks every stock item at once.
That is deliberate: the gap is in the data, not in a material.

## Where it is read

The feature builder resolves the window once per build and records it on `i7_feature_run` (`window_status`,
`window_start`, `window_end`, `window_missing_months`, `window_trailing_months`).

* **Features** (`features/builder.py::_build_one`): the demand statistics, ADI / CV-squared, class and routing of
  non-OAR rows are computed over the window; `window_history_status` stores the history gate over it.
* **Forecasting** (`adapters/repository.py::consumption_series_for`): reads the window back from the run. A
  `MISSING_DATA` window gives no series. A run recorded before this change falls back to the extract's own window.
* **Inventory**: reads the statistics the builder stored, so safety stock, ROP and max stock follow the same window.
* **Recommendations** (`recommendations/builder.py::build_normal_recommendation`): blocks on `MISSING_DATA`.

## What OAR still sees

OAR reads the feature store, so the window is kept away from the columns it reads:

* `history_status` stays the gate over the whole extract. A donor is a material-plant with
  `history_status = 'SUFFICIENT'`; recomputing it over 24 months would have cut the material-plants that pass it by
  about 80% on the July extract (8,573 to 1,562), and with them the pool OAR draws donors from. `window_history_status` is the new, separate gate for classification and forecasting.
* OAR material-plants are built exactly as before (a test compares the whole row with and without the window).
* `consumption_count_12m` (the conversion trigger, 6 months) still ends at the extract's last staged month.
* `oar/`, `policy/oar.py`, `features/oar_scope.py` and the OAR branch of the recommendation builder are unchanged.

## Rollout

1. Apply migration `e6a3c91d5b28` (`alembic upgrade head`): adds the run columns and
   `i7_material_feature.window_history_status`. All nullable; before it is applied, readers fall back to the old window.
2. Re-run the pipeline: features, forecasting, inventory, recommendations.
3. Audit it, read-only:

```bash
python -m scripts.i7_window_audit window                 # before vs after eligibility, without a rebuild
python -m scripts.i7_window_audit runs                   # the latest two inventory runs, side by side
```

## Limits worth knowing

* Completeness is inferred from volume, not proven by SAP. It catches a collapse (June at 14% of May); it cannot
  catch a month that is short by a little, and it cannot tell a shutdown from a gap, which is what
  `I7_DEMAND_VALIDATED_MONTHS` is for. A control total from the extract job (movement count per month) would replace
  the inference.
* The 25% ratio is a first setting, not calibrated.
* One verdict per month for all plants. A plant whose data stops earlier than the others is judged on the combined volume.
* Not changed, and still on their own windows: the declining-demand rule, the stockout-risk usage, the OAR demand
  sanity check, and the quarterly report's source-activity section (which still reads any month with rows as
  available and a gap between two staged months as a confirmed zero).
* The recommendation detail's consumption chart is empty while the window is `MISSING_DATA`.

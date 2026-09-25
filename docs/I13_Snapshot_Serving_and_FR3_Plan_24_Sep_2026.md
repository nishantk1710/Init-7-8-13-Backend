# Initiative 13 — Snapshot Serving and FR-3 Quantity Suggestion: Implementation Plan

| | |
|---|---|
| **Date** | 24 September 2026 |
| **Status** | PLAN ONLY — nothing in this document is implemented |
| **Backend** | `Init-7-8-13-Backend`, branch `feat/vp/ws7-assistant` |
| **Reference** | `Initiative_13_FRS_v1.2_final.docx` (FR-1, FR-3, FR-6, §7), `Initiative_13_Current_State_23_Sep_2026.md` |

---

## 0. What this plan fixes

Five problems, found by reading the code against FRS v1.2:

| # | Problem | Evidence in code |
|---|---|---|
| **P1** | Most I13 reads recompute everything from the raw tables on every request. Only `/act/utilisation`, `/act/exceptions` and the assistant trace read stored results. | `/summary` ~4 min, `/watch` ~12 s, `/exceptions` 14–18 s; `/ledger`, `/utilisation-ledger*` and `/consumption-attribution` build everything and then slice |
| **P2** | The data is a **static seeded snapshot** (`python -m app.seed`). Recomputing gives the same answer every time until someone reseeds, so the work is repeated for nothing. | No I13 endpoint calls SAP. `ingestion_run` already records every load |
| **P3** | FR-3 inputs are recomputed live, and even a single-material call scans whole tables | `api/i13/assistant.py:291` and `assistant/session.py:159` call `compute_watch_metrics`. EKBE (`procurement_chain.py:235`), GI candidates (`:240`) and GI-by-reservation (`reservation_ledger.py:202`) get no material filter |
| **P4** | FR-3 logic gaps against the FRS | Plan window ignored; "nudge up" not built; `I13_QUANTITY_LOOKBACK_MONTHS` appears only in the reason text (`quantity.py:205,214,222`) while the calculation uses `I13_CONSUMPTION_WINDOW_MONTHS` (`watch.py:186`); open POs come from EKPO−EKBE, not EKET |
| **P5** | The 12-month window is anchored to **today**, not to the end of the data | `movement_metrics.py:69`: `months_before(as_of, window)` with `as_of = date.today()`. MKPF ends 20-Aug-2026, so ~11 months of issues are divided by 12, and the window keeps sliding over data that never changes |

**The core idea for P1 + P2:** compute once per data snapshot, store the results, and serve every read from the stored results. Recompute only when the snapshot changes (a reseed) or the configuration those results depend on changes.

---

## 1. Decisions needed before building

| ID | Decision | Options | Recommendation |
|---|---|---|---|
| **D1** | What date "today" means for the metrics | (a) Snapshot date = latest `raw_mkpf.posting_date` (20-Aug-2026) · (b) Wall-clock today | **(a).** It is what makes the results stable, and therefore cacheable. Days since last movement, the 12-month window and aging all become "as of the data". Show "Data as of 20-Aug-2026" on every screen. With (b) the marts go stale every day and P2's premise no longer holds. FRS §6 supports (a): "the assistant answers from the latest available state". |
| **D2** | FR-3 "nudge up where it falls short of the plan need" | (a) Build it, behind a flag, default off · (b) Build it, default on · (c) Don't build it, record the deviation | **(a).** The current cap on the requested quantity exists so FR-3 doesn't turn into a second reorder engine (I07's job). A flag lets VZI decide without another release. |
| **D3** | Which FR-3 implementation survives: `quantity.py` (this branch) or `quantity_suggestion*.py` (`feat/sj/init13`) | — | Keep **`quantity.py`** as the engine (pure, already used by the conversation). Take the endpoints, acceptance and justification from `feat/sj/init13` only if the frontend needs them. Must be settled before Phase 3. |
| **D4** | Where the refresh code comes from | Port `app/refresh_marts` + `sql_filters.py` from `feat/sj/init13`, or write new | **Port.** Only `__pycache__` exists here; the source is on the other branch. |
| **D5** | Legacy `/i13/exceptions` (W6.3, no longer used by the UI) | Put it on a mart · leave it live · deprecate | **Leave it live, filters required.** Then deprecate. Not worth a mart. |
| **D6** | Should a stale mart be served? | Serve, marked stale · refuse with 503 · recompute automatically | **Serve, marked stale** (`stale: true`, `snapshotId`, `dataAsOf` in the response). A 4-minute recompute must never happen inside a request. |

---

## 2. Phase 0 — Branch consolidation (prerequisite)

1. Merge or cherry-pick from `feat/sj/init13`: `app/refresh_marts/`, `app/integrations/sap/sql_filters.py`, the `?live=true` parameter on `/summary` and `/reclassification`, and the reclassification mart read path.
2. Settle D3 and remove the URL collision on `/i13/quantity-suggestion`.
3. Fix drift #1 from the Current State doc: the frontend calls `/i13/quantity-suggestion/compute`, which does not exist.

**Exit:** one branch, one FR-3 engine, `python -m app.refresh_marts` exists as source.

---

## 3. Phase 1 — Snapshot identity and refresh (P2)

### 3.1 A data-snapshot fingerprint

Add `app/shared/data_snapshot.py`:

- `current_snapshot(db) -> DataSnapshot` with:
  - `snapshot_id`: SHA-256 over the sorted `(target_table, source_file, source_sha256, row_count)` of **every** successful `ingestion_run` row for the tables I13 reads (`raw_marc`, `raw_mard`, `raw_mseg`, `raw_mkpf`, `raw_resb`, `raw_eban`, `raw_ekpo`, `raw_ekbe`, `raw_eket`, `raw_zmm065_*`).
  - `data_as_of`: `MAX(raw_mkpf.posting_date)`. This is the D1 anchor.
- Use **all** successful runs per table, not the latest one. This is the bug behind blocker B8: MSEG was loaded in two batches with the same timestamp, and taking only the latest run under-reports it by 56%.

### 3.2 A config fingerprint

The marts also depend on configuration: aging thresholds, `I13_CONSUMPTION_WINDOW_MONTHS`, OAR/Min-Max MRP types, the GRNI threshold, and the plant scope. Add `config_fingerprint(I13Config)`, a hash of exactly those fields. If a value changes, the marts are stale even though the data isn't.

### 3.3 A refresh-run record

New table `i13_mart_refresh_run` (Alembic migration):

| Column | Purpose |
|---|---|
| `id`, `started_at`, `finished_at`, `status` (`RUNNING`/`SUCCEEDED`/`FAILED`), `error` | Run bookkeeping |
| `snapshot_id`, `config_fingerprint`, `data_as_of` | What the marts were computed from |
| `row_counts` (JSON) | Rows per mart, for the data-sources panel |

The latest `SUCCEEDED` row is the **serving snapshot**.

### 3.4 One refresh entry point

`python -m app.refresh_marts` (ported, then extended):

1. Read `current_snapshot` and `config_fingerprint`. If they match the last successful run, **exit with nothing to do** (use `--force` to override).
2. Build shared repositories **once**, so the per-instance request cache spans the whole refresh.
3. Refresh, in dependency order, inside **one transaction** (readers see either the old snapshot or the new one, never a mix):
   1. `i13_monthly_consumption` (new, §4.2)
   2. `i13_procurement_chain_mart` (new, W6.1)
   3. `i13_utilisation_ledger_mart` (new, W6.2)
   4. `i13_watch_metric_mart`: **full population, both plants, `oar_only=False`** so `/watch` can serve every scope from it. This fixes blocker B6 (16% coverage).
   5. `i13_reclassification`
   6. `i13_consumption_attribution`
4. Write the `i13_mart_refresh_run` row.

Hooks:

- `python -m app.seed` runs the refresh at the end (skip with `--no-refresh`).
- API startup **logs a warning** when the serving snapshot ≠ the current snapshot. It does not refresh automatically (D6).
- `POST /api/i13/run/refresh-marts` for the manual button, listed in `tests/test_write_paths.py` with its reason. This is also the seam a future daily Azure job would call (FR-6).

### 3.5 Snapshot-independent inputs: captured consumption plans

Plans captured through the assistant land in `consumption_plan` **without any reseed**, and WATCH's `acquired_vs_plan_status` depends on them. Storing that status in the mart would make it wrong the moment a plan is captured.

**Approach: overlay at read time.** The mart keeps the snapshot-derived `received_quantity`. The read path sums planned quantity from `consumption_plan` (small, indexed) and runs the existing `_acquired_vs_plan` / `_acquired_vs_plan_variance` functions. This is a pure function of stored values, so nothing is re-derived from raw tables. Move those two helpers out of `watch.py` into a small shared module so the mart and the overlay use the same code.

### 3.6 Also fix the unfiltered queries (P3)

Even with marts, the refresh and `?live=true` should not scan whole tables for a single material:

- `fetch_goods_receipt_history`: add `material` / `plant` filters (join `raw_ekpo` on `purchasing_document` + `item`).
- `fetch_deterministic_gi_candidates`, `fetch_goods_issue_by_reservation`: add `m.material` / `m.plant` filters.
- Pass `material` / `plant` through from `build_procurement_chain` and `build_reservation_ledger`.
- `load_consumption_plans`: filter by material and plant when given.

---

## 4. Phase 2 — Serve reads from the marts (P1)

### 4.1 Endpoint by endpoint

Every endpoint keeps its **response shape** (the frontend contract stays the same). It gains `snapshotId`, `dataAsOf` and `stale` (in the body or as `X-I13-*` headers; body is preferred where the envelope allows it) and keeps `?live=true` as a parity and debugging escape hatch.

| Endpoint | Today | Serve from | Notes |
|---|---|---|---|
| `/i13/summary` | full recompute, ~4 min | SQL `COUNT`s over `i13_watch_metric_mart`, `i13_act_exception`, `i13_reclassification`; OAR total from `raw_marc` | **Parity risk:** the summary counts OAR positions with **no activity at all** as NON_MOVING, but WATCH has no row for them. Count them as `OAR total − WATCH OAR rows` and add them to NON_MOVING, then verify against live |
| `/i13/watch` | live, no paging | `i13_watch_metric_mart` + plan overlay | Add `limit` / `offset` |
| `/i13/movement-metrics` (+ per material) | live W3.5 | `i13_watch_metric_mart` (all fields are already there) | 404 semantics unchanged |
| `/i13/utilisation-ledger*`, `/partial`, `/partial/diagnostics` | build all, then slice | `i13_utilisation_ledger_mart`, `i13_procurement_chain_mart` | Filtering and paging in SQL. Diagnostics become `GROUP BY` queries |
| `/i13/ledger` (compat) | build all, then slice | map from `i13_utilisation_ledger_mart` via `ledger_compat.py` | |
| `/i13/consumption-attribution*` | live | existing `i13_consumption_attribution` mart (currently read by nothing) | |
| `/i13/reclassification` | live here (mart on the sj branch) | `i13_reclassification` | From Phase 0 |
| `/i13/validation` | live chain | counts from `i13_procurement_chain_mart` | |
| `/i13/quantity-suggestion`, `POST /assistant/sessions` (I13 assessment) | live `compute_watch_metrics` | `get_watch_metric()` + `i13_monthly_consumption` | See §5. Add `watch_metric_from_row()` so `suggest()` and `reservation_assistant.build()` keep taking a `WatchMetric` |
| `/act/utilisation`, `/act/exceptions` | already on marts | unchanged | Add the missing `limit` / `offset` (drift #3) |
| Cross-plant stock | small live `raw_mard` query | unchanged | Already cheap |
| `/i13/exceptions` (W6.3 legacy) | live | unchanged (D5) | Require at least one filter, or document the cost |

### 4.2 New mart: `i13_monthly_consumption`

`(material, plant, month)` → `issued_qty_net`, `issue_count`, `receipt_qty`, reversals already netted (the vocabulary in `movements.py`). Built in one SQL `GROUP BY` over `raw_mseg` ⨝ `raw_mkpf`.

It exists so FR-3 can use **any look-back** (§5.1) without touching raw tables. It also gives the demo-only *Consumption / Usage Pattern* screen a real source later.

### 4.3 404 semantics on a mart read

- Mart fresh and no row → the same message as today ("no movement or ledger activity… a data gap, not a zero").
- Mart stale or never refreshed → a different message: "WATCH has not been refreshed for the current data snapshot; run the refresh". A missing refresh must never be presented as missing data.

---

## 5. Phase 3 — FR-3 corrections (P4, P5)

### 5.1 Make the look-back real

- The rate comes from `i13_monthly_consumption` over the last `I13_QUANTITY_LOOKBACK_MONTHS` ending at `data_as_of`:
  `rate = Σ issued_qty_net ÷ lookback_months`, and `consumption_count = Σ issue_count` over the same months.
- Validate at startup, and report per suggestion, when the look-back is longer than the months of data available (the current extract has ~12.5 months). When that happens, refuse with the new reason `LOOKBACK_EXCEEDS_HISTORY` instead of dividing by months that contain no data.
- `WatchMetric` gets two new fields, used only by FR-3: `lookback_consumed_qty` and `lookback_consumption_count`. The rest of WATCH keeps its own window.

### 5.2 Anchor to the snapshot date (D1)

Pass `as_of = data_as_of` everywhere I13 computes: refresh, `?live=true`, the assistant and the suggestion. Every suggestion stores the `data_as_of` and `snapshot_id` it was based on.

### 5.3 Target the stated plan window (FRS: "suggest the quantity that covers the stated plan window")

```
available        = stock_on_hand + open_po_quantity
window_months    = (window_end − window_start).days ÷ 30.4375      # only when both dates are given
window_need      = rate × window_months
ceiling_target   = rate × cover_ceiling_months

base             = max(window_need − available, 0)                 # what the window still needs
cap              = max(ceiling_target − available, 0)              # never past the ceiling
suggested        = floor(min(base, cap))

if requested > suggested        → direction = DOWN   (keeping it is an override → justification, as today)
if requested < suggested:
     NUDGE_UP_ENABLED (D2)      → direction = UP     (suggest `suggested`)
     otherwise                  → suggested = requested, direction = NONE   (today's cap)
```

- **No window captured** (the dates are optional in FR-4): fall back to today's behaviour, target = ceiling. Record `target_basis = CEILING_ONLY` versus `PLAN_WINDOW` so the two can always be told apart.
- The refusals stay first and in the same order: history, then rate.
- Rounding stays **down**. The ceiling still can't be exceeded.
- The reason sentence names the window, the need and the direction.

**Worked examples** (rate 2/month, stock 3, open PO 1 → available 4, ceiling 12 months → cap 20):

| Window | Need | Base | Suggested | Requested 15 | Requested 5 |
|---|---:|---:|---:|---|---|
| 6 months | 12 | 8 | **8** | DOWN to 8; keeping 15 is an override | UP to 8 if the flag is on, otherwise 5 |
| 18 months | 36 | 32 | **20** (the ceiling applies) | UP to 20 if the flag is on, otherwise 15 | UP to 20 if the flag is on, otherwise 5 |
| none | — | — | **20** (ceiling only) | 15 (capped at the request, as today) | 5 (as today) |

> Row 2: a long window can push the suggestion above the request, but never past the cover ceiling. Row 3: with no window there is no stated plan need, so nudge-up never applies, whatever the flag says.

### 5.4 Open-PO netting from EKET (FRS §7)

- New read `fetch_open_schedule_lines(material, plant)` over `raw_eket`: `scheduled_quantity` (MENGE), `qty_delivered` (WEMNG), `delivery_date` (EINDT). Reuse the column mapping from I08's `sql/22_v_eket.sql`.
- `open_po_quantity = Σ max(menge − wemng, 0)` per schedule line; PO items with no EKET row fall back to EKPO−EKBE. Add `earliest_open_delivery_date` to the basis.
- **Before switching:** a reconciliation script comparing EKET-based and EKPO−EKBE-based totals per material and plant on the seeded data. Also confirm whether deleted items (`EKPO.LOEKZ`) and delivery-complete items (`ELIKZ`) are excluded today. If they aren't, that is a separate over-count to fix in the same step.
- Keep `open_po_source = EKET | EKPO_EKBE` on the metric.

### 5.5 MCHB batch stock (FR-1): verify before changing

In standard SAP, `MARD.LABST` already **includes** batch stock (MCHB is the batch-level breakdown of it). Adding `MCHB.CLABS` on top would double-count. Plan:

1. Run a reconciliation query: `Σ MCHB.CLABS` vs `MARD.LABST` per material, plant and storage location.
2. If they match: no code change. Close the FR-1 gap as "covered by MARD" and record it for FRS v1.3.
3. Only if MARD is short for batch-managed materials: add the difference, with `stock_source` recorded.

### 5.6 Persisting the new basis

Alembic migration adding **nullable** columns to `quantity_suggestion` (append-only triggers block UPDATE/DELETE, not ALTER): `window_months`, `window_need`, `target_basis`, `direction`, `open_po_source`, `data_as_of`, `snapshot_id`. Past rows keep NULL, which honestly records that the basis wasn't captured then. Mirror the new fields on `QuantitySuggestionResponse` (camelCase) and in the session trace.

### 5.7 New config

| Setting | Default | Source |
|---|---|---|
| `I13_QUANTITY_NUDGE_UP_ENABLED` | `false` | **ours**, D2, awaiting VZI |
| (existing) `I13_QUANTITY_LOOKBACK_MONTHS` | `12` | **ours**, now actually used |

---

## 6. Phase 4 — Verification

| Check | How |
|---|---|
| **Mart = live parity** | For each endpoint in §4.1, a Postgres test comparing the mart response with `?live=true` on the seeded DB: full population for counts, a sample of ~50 material-plants field by field. The summary no-activity NON_MOVING count gets its own test |
| **Staleness** | Tests for: fingerprint unchanged → refresh is a no-op; reseed → `stale: true`; config change → `stale: true`; a failed refresh leaves the previous snapshot serving |
| **Plan overlay** | Capture a plan through the assistant → `/watch` shows `acquired_vs_plan` updated with no refresh |
| **FR-3 unit tests** (`tests/i13/test_quantity.py`) | Each row of the §5.3 worked-example table; look-back 6 vs 12 gives different rates (a regression test for the decorative setting); `LOOKBACK_EXCEEDS_HISTORY`; no window → `CEILING_ONLY`; nudge-up flag on/off; refusal order unchanged; null ≠ zero kept |
| **EKET / MCHB** | The two reconciliation scripts from §5.4 / §5.5, results written into this document before the switch |
| **Performance budget** | `/summary` < 1 s, `/watch?limit=100` < 0.5 s, `/quantity-suggestion` < 0.3 s, `POST /assistant/sessions` (I13) < 0.5 s; full refresh time recorded (expected: minutes, once per snapshot) |
| **Test suite time** | The Postgres suite refreshes once in a session fixture instead of each test recomputing. The current ~17 min should drop sharply; record the new figure |

---

## 7. Frontend impact

- Show "Data as of {dataAsOf}" and a **stale** banner on every I13 screen, replacing the per-screen `calculatedAt` staleness note.
- Fix drift #1 (`/compute`) and drift #3 (`limit` / `offset` on `/act/*`).
- Quantity step: show target basis (plan window vs ceiling only), window need, and direction (DOWN/UP).
- A "Refresh data" action (Server Action → `POST /api/i13/run/refresh-marts`), separate from the existing "Run detection" button. It must not retry.

---

## 8. Order of work and rough size

| Step | Phase | Size | Depends on |
|---|---|---|---|
| 1 | Branch consolidation, D3/D4 | M | — |
| 2 | Snapshot and config fingerprints, `i13_mart_refresh_run`, refresh CLI, seed hook | M | 1, D1 |
| 3 | Material filters on the EKBE and GI queries | S | — (can start at once) |
| 4 | `i13_monthly_consumption`, full-population WATCH mart, plan overlay | M | 2 |
| 5 | Serve `/watch`, `/movement-metrics`, `/quantity-suggestion`, the assistant assessment and `/summary` from marts | M | 4 |
| 6 | FR-3: real look-back, snapshot anchoring, plan-window target, nudge-up flag, new persisted fields | M | 4, D2 |
| 7 | Ledger and procurement-chain marts; serve the ledger, attribution and validation endpoints | L | 2 |
| 8 | EKET netting (after reconciliation), MCHB verification | S–M | 6 |
| 9 | Frontend changes | M | 5, 6 |
| 10 | Parity, staleness and performance verification; update the Current State doc and the FRS traceability | M | all |

Steps 5 and 6 together fix the quantity suggestion end to end and can ship before step 7.

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Mart and live responses differ subtly (the summary no-activity rows, the scope mix) | Parity tests with `?live=true` kept permanently |
| Snapshot anchoring (D1) changes today's numbers: the rate goes up ~8%, days-since go down ~35 days | Announce it; show `dataAsOf` everywhere; the numbers become *more* correct |
| A long refresh holds a transaction open | Build new rows in staging tables and swap them in one short transaction if the single-transaction refresh is too slow |
| Nudge-up conflicts with I07 ownership | Off by default (D2) |
| EKET changes open-PO figures | Reconcile first; keep `open_po_source` on every metric |
| The live `SAP OData` path returns later and the data stops being static | The fingerprint is the seam: a delta load writes `ingestion_run`, the snapshot changes, the refresh runs. No redesign needed |

---

## 10. Out of scope

- UoM-aware rounding (a known simplification, unchanged).
- Blockers that code can't close: B1 (reservation→PR linkage), B2 (session field on the reservation), B3 (MARC for plant 1500), B4 (25-month history re-extract).
- Wiring ACT owners to W6.4 attribution (B0). It is still the highest-value separate fix.
- A scheduler. §3.4 gives it one entry point to call.

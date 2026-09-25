# Spares AI — Every Screen Live from Postgres, Instantly, and Wired to the Assistant: Plan

| | |
|---|---|
| **Date** | 24 September 2026 (revision 3, same day) |
| **Strategy (rev 3)** | **I13 is served the way I8 already is.** Each backend process builds one **in-memory snapshot** from the raw tables, then answers every read from memory. Data users write (plans, exceptions, sessions, justifications) is read live from its tables and applied on top. Postgres mart tables are deferred to Phase B (§4.7). Wherever §2 and §3 name a mart table as a "target source", Phase A reads the matching snapshot component listed in §4.2 |
| **Status** | **Phase A implemented for I13 on 24-Sep** (uncommitted). What was built, measured and deferred is in §0.1. I8 was deliberately not touched |
| **Backend** | `Init-7-8-13-Backend`, `feat/vp/ws7-assistant` |
| **Frontend** | `Init-7-8-13-Frontend`, `feat/vp/ws7-assistant-frontend` |
| **Builds on** | `I13_Snapshot_Serving_and_FR3_Plan_24_Sep_2026.md`. Its §3 (snapshot fingerprint + refresh) and §5 (FR-3) still apply. **This document replaces its §4 (serving) and §8 (order of work).** |
| **Scope change in rev 2** | Rev 1 covered only `/oar-utilization`. Rev 2 covers **every frontend route** (OAR, assistant, repairable spares, inventory planning, home/actions/approvals/audit/materials). It also adds how the **reservation assistant feeds the dashboards** (§3) and a **route → table / calculation map** for every backend endpoint (§2) |

**Goal:**
1. Every screen shows its layout at once and its data in **under one second**.
2. The data comes **from Postgres**, not fixture files.
3. Anything the reservation assistant records shows up on the dashboards it affects **on the next page view**.

---

## 0. Environment state (fixed on 24-Sep, before measuring)

| Item | Was | Now |
|---|---|---|
| Postgres on 127.0.0.1:5432 | Rev 1 found it down | **Up.** The API reaches it |
| Alembic | DB at `d8c3b1f04e75`. `assistant_session.department` / `requested_for` were missing, so every `POST /assistant/sessions` returned 500 | `d3b1c7a94e52` stamped: its tables already existed and matched exactly. Upgraded to head `f2a7c19d4b83` |
| I8 SQL views (`v_ekpo`, `v_mara`, … 11 views) | **Missing**, so `/i8/snapshot`, `/i8/register`, `/i8/declarations`, `/i8/coding-candidates` returned 500 in 0.2 s | Created with `python -m app.initiatives.i8.views create`. **Alembic does not create them.** Step 0 makes this automatic |
| `app/refresh_marts/` | — | **Only `.pyc` files, no `.py` source**. Same for `app/integrations/sap/sql_filters.py`. The source is on `feat/sj/init13`. Nothing in the shipped app refreshes any mart |

### 0.1 Implementation status (I13 only, 24-Sep)

**Built**

| Plan item | Where |
|---|---|
| In-memory I13 snapshot: background build at start-up, lazy build when nothing started it, 503 `building` + `Retry-After`, 60 s fingerprint check (reseed or new day), `GET /api/i13/snapshot`, `POST /api/i13/snapshot/refresh` | `app/initiatives/i13/snapshot.py`, `app/main.py` (lifespan, headers, CORS `expose_headers`), `app/api/i13/deps.py` |
| Every I13 read route served from the snapshot; `?live=true` keeps the old compute | `app/api/i13/*.py` |
| `X-Total-Count` on every list route; `/act/exceptions` paged in SQL | `deps.page`, `act_exception_store.list/count` |
| New `/i13/grni`, `/i13/usage-patterns`, `/i13/act/confirmations` | `app/api/i13/usage.py`, `act.py` |
| Reclassification criticality batched with `get_many`: **108 s to 0.6 s**. This is what made `/reclassification` and `/summary` exceed 90 s | `initiatives/i13/reclassification.py` |
| G1: WATCH built with captured plans; `refresh_key` after a capture | `snapshot.py` |
| G2: summary and the legacy queue read live plans | `summary_from_snapshot`, `exception_queue` |
| G3/G4: unlinked captured plans match by material, plant and window; breach measured from window end | `plans.PlanMatcher`, `exceptions.py`, `act/service.py`, `act/detection.py` |
| G5: detection also reads the assistant's quantity decisions | `quantity_suggestion_store.build_assistant_quantity_decision_records` |
| G6/§3.4: on completion, an I13 conversation refreshes its WATCH row and runs scoped detection | `app/assistant/downstream.py`, `turns.py` |
| G10: assistant assessment reads the snapshot's WATCH row | `assistant/session.py` |
| Detection shares one runner and reads the snapshot, not the stale mart | `initiatives/i13/act_runner.py` |
| D14: one worker by default | `startup.sh` |
| Frontend: `loading.tsx` (OAR, assistant); GET timeout; "being prepared" message on 503; "N of TOTAL" row notes; 30-Day GR-Not-Issued and Usage Pattern screens and nav; justifications in 2 requests instead of up to 33; reclassification page lists candidates only; finished conversation links to Plans, WATCH, Exceptions and the trace, and revalidates them; sessions filter (G8) | `Init-7-8-13-Frontend` |

**Measured after the change** (seeded data, same machine):

| Measure | Result |
|---|---|
| Full snapshot build | 40 s, about 1.3 GB |
| Every I13 list route | 10–80 ms |
| `/ledger?limit=1000` | 0.54 s |
| `/summary` | 20 ms (was over 90 s) |
| Assistant capture → WATCH row updated and 20 NO_PLAN exceptions resolved | about 0.15 s |

**Tests:**
- **New `tests/i13/test_snapshot.py`:** matcher, window-end breach, exception-queue parity, monthly netting, 503, and snapshot-vs-live parity on Postgres.
- **Backend I13, assistant and write-path suites:** all green except the two mart partial-refresh tests, which fail identically on untouched `HEAD` (data precondition).
- **Frontend:** `tsc` (one pre-existing I8 test-file error), lint, vitest and `next build` all pass.

**Not done in this pass**

| Item | Why |
|---|---|
| Redeployment screen and the Overview's three demo charts | Need the VZI rule (D8); still fixture-backed, banner kept |
| `/home`, `/actions`, `/audit`, `/materials`, anything I8 or I7 | Out of scope (I13 only) |
| D12: retire one of the two quantity-suggestion tables | Detection now reads both instead; the table decision is still open |
| `<Suspense>` per dashboard section, URL paging past 1,000 rows | Every section now answers in under 100 ms, so the all-or-nothing wait is gone. Screens still cap at 1,000 rows, now with the true total shown |
| Phase B (persisted marts), §4.5 build speed-ups | Not needed at 40 s / 1.3 GB |

---

## 1. Measured today (24-Sep, `curl` against the running API, 90 s cut-off)

| Endpoint | Status | Time | Payload | Notes |
|---|---|---|---|---|
| `/i13/summary` | — | **> 90 s** (rev 1: ~4 min) | — | Live recompute of everything |
| `/i13/reclassification` | — | **> 90 s** | — | Live recompute. The `i13_reclassification` mart has 44,394 rows but no endpoint reads it |
| `/i13/validation` | 200 | **16.9 s** | 0.4 KB | Full procurement chain + exception queue, just to count |
| `/i13/watch` | 200 | **11.8 s** | **12.9 MB** | Live. No page uses it |
| `/i13/ledger?limit=1000` | 200 | **9.2 s** | 646 KB | Builds all, then slices |
| `/i8/snapshot` (first call after restart) | 200 | 3.0 s | 1.5 KB | Builds the in-memory I8 snapshot once per process |
| `/i13/act/exceptions?limit=1000` | 200 | 1.24 s | 710 KB | Reads all **42,649** rows, then slices in Python |
| `/i8/coding-candidates` | 200 | 0.89 s | 48 KB | Keyword pass (cached after first build) |
| `/i13/act/utilisation?limit=1000` | 200 | 0.38 s | **1.05 MB** | Mart read, sliced in Python |
| `/i8/register?pageSize=500`, `/i8/declarations?pageSize=500` | 200 | 0.25 s | 546 / 282 KB | In memory after the snapshot |
| `/i13/consumption-plans`, `/assistant/sessions`, `/justifications`, `/i13/data-sources`, `/i8/attestations` | 200 | 0.22–0.25 s | < 2 KB | Small tables |

**Table sizes:**

| Table | Rows |
|---|---|
| `raw_mseg` | 233,145 |
| `raw_ekbe` | 157,266 |
| `raw_resb` | 105,848 |
| `raw_marc` | 45,409 |
| `i13_act_exception` | 42,649 |
| `i13_reclassification` | 44,394 |
| `i13_consumption_attribution` | 41,161 |
| `i13_watch_metric_mart` | **7,184** (1300: 7,152, 1200: 32; a partial population) |
| `assistant_session` | 3 |
| `consumption_plan` | 2 |
| `justification` | 1 |
| `i13_quantity_suggestion`, `i13_act_confirmation`, `i8_attestation` | 0 |

---

## 2. Where every screen gets its data

### 2.1 Frontend route → API → backend source

The **Source today** column uses these codes:
- **LIVE**: computed per request from `raw_*` tables, aggregated in Python.
- **TABLE**: an indexed read of an app table or mart.
- **FIXTURE**: a TypeScript file in the frontend; no backend call.
- **SNAPSHOT**: the I8 in-memory snapshot, built from the `v_*` views once per process.

Every live page calls `await connection()` (dynamic render, no cache).

#### Reservation assistant

| Route | API calls | Source today | Target source |
|---|---|---|---|
| `/assistant` | `GET /assistant/ask/suggestions`, `POST /assistant/ask` | TABLE (GROUP BY on `i13_act_exception`) / SNAPSHOT (I8 intent) | same |
| `/assistant/new` | `POST /assistant/sessions`, `POST /assistant/sessions/{id}/turns` | **LIVE** for I13: `compute_watch_metrics` + cross-plant `raw_mard`, **rebuilt on every turn**. SNAPSHOT for I8. Writes the tables in §3.1 | Read the `i13_watch_metric_mart` row + `i13_monthly_consumption` (§4.2). Build the assessment once per session, not per turn |
| `/assistant/sessions` | `GET /assistant/sessions?limit=200`, `GET /justifications?limit=200` | TABLE (`assistant_session` + an **N+1** `assistant_turn` query per row; `justification`) | TABLE, one joined query, paged, true total. **Bug:** the `?material&plant` filter sent by `SessionChips` is ignored by the page |
| `/assistant/sessions/[id]` | `GET /assistant/sessions/{id}` | TABLE (`assistant_session`, `assistant_turn`, `consumption_plan`, `quantity_suggestion`, `justification`) | same |

#### OAR utilization (Initiative 13)

| Route | API calls | Source today | Target source |
|---|---|---|---|
| `/oar-utilization` (Overview) | `/i13/summary` + `/i13/act/utilisation?limit=1000`, all-or-nothing `Promise.all`. **Plus a fixture** (`overview-metrics.ts`: department value, NM/SM inflow, redeployment avoidance) with a demo banner | **LIVE** (> 90 s) + TABLE | `i13_summary_snapshot` + live ACT counts. The three fixture charts come from `i13_monthly_consumption` / `i13_redeployment_candidate` |
| `/oar-utilization/utilisation-dashboard` | 7 sections: summary, WATCH, reclassification, exceptions (+ sessions), justifications (1 + 2 + **up to 30** detail calls), plans, validation | Mix; the page waits for the slowest (`/summary`) | Each section from its mart, streamed with `<Suspense>`. Justifications from the new `/i13/act/confirmations` |
| `/oar-utilization/watch` | `/i13/act/utilisation?…&limit=1000` + `/assistant/sessions?flow=i13` | TABLE (`i13_watch_metric_mart`, partial population, no refresh job) | Full-population mart, paged in SQL, refreshed per snapshot **and per assistant capture** (§3.3) |
| `/oar-utilization/ledger` | `/i13/ledger?limit=1000`, `/i13/data-sources` | **LIVE** (9.2 s): `build_legacy_ledger` over `raw_eban`, `raw_ekpo`, `raw_ekbe`, `raw_mseg`/`raw_mkpf`, `raw_resb`, `raw_marc` | `i13_utilisation_ledger_mart` |
| `/oar-utilization/aging-exceptions` | `/i13/act/exceptions?limit=1000` + sessions + `/justifications?limit=1`. Writes via `confirmExceptionAction` | TABLE (`i13_act_exception`), but all 42,649 rows are read, then sliced | Same table, paged in SQL, true total |
| `/oar-utilization/plans` | `/i13/consumption-plans?limit=500` | TABLE (`consumption_plan`) | same (already fast) |
| `/oar-utilization/reclassification` | `/i13/reclassification` | **LIVE** (> 90 s): `raw_mseg`/`raw_mkpf`, `raw_mard`, `raw_marc`, `raw_zmm065_*` | The existing `i13_reclassification` mart (44,394 rows) |
| `/oar-utilization/validation` | `/i13/validation` | **LIVE** (16.9 s) | `GROUP BY` over `i13_procurement_chain_mart` |
| `/oar-utilization/redeployment` | none | **FIXTURE** `redeployment.ts`, demo banner | `/i13/redeployment` over `i13_redeployment_candidate` (rule needs VZI sign-off, D8) |
| `/oar-utilization/gr-not-issued` (30-Day GRNI) | none (the route is not on this branch) | **FIXTURE** / absent | `/i13/grni` over `i13_utilisation_ledger_mart` |
| `/oar-utilization/usage-patterns` | none (the route is not on this branch) | **FIXTURE** / absent | `/i13/usage-patterns` over `i13_monthly_consumption` |

#### Repairable spares (Initiative 8)

| Route | API calls | Source today | Target source |
|---|---|---|---|
| `/repairable-spares` (Overview) | none | **FIXTURE** (`repair-chains.ts`, `declarations.ts`). The demo banner was removed in `fba5ad3`, but the comment still claims one | `GET /i8/snapshot` + new `/i8/overview` counts from the SNAPSHOT |
| `/repairable-spares/repair-register` | `/i8/register?pageSize=500`, **paged sequentially until complete**, then `/i8/snapshot` | SNAPSHOT (`v_ekpo`, `v_eket`, `v_ekbe`, `v_mseg`, `v_ekko`, `v_lfa1`, `v_marc`) | same source; server-side paging instead of fetching every page |
| `/repairable-spares/repair-register/[id]` | `/i8/register/{doc}/{item}` | SNAPSHOT + `i8_attestation` | same |
| `/repairable-spares/declarations` | `/i8/declarations` (sequential pages) + `/i8/attestations`. Writes `POST /i8/attestations` → `router.refresh()` | SNAPSHOT + TABLE (`i8_attestation`) | same, paged |
| `/repairable-spares/coding-candidates` | `/i8/coding-candidates`; "Run AI screen" → `?screen=true` (multi-minute LLM call, from the browser) | LIVE text screen over `v_ekpo`, cached per process | Persist screen results in a table; the AI screen becomes a background job |
| `/repairable-spares/duplicate-guard` | none | **FIXTURE** `REPAIR_CHAINS` | SNAPSHOT (open repairs per material) via `/i8/register?openOnly=true&material=` |
| `/repairable-spares/justifications` | `/justifications?kind=NEW_ACQUISITION` | TABLE (`justification`) | same |

#### Inventory planning (Initiative 7): no backend exists

The I7 router in `app/api/router.py` is commented out and `app/initiatives/i7/` is a stub.

| Route | Source today | Target |
|---|---|---|
| `/inventory-planning`, `/pipeline`, `/recommendations`, `/recommendations/[id]` | **FIXTURE** (`recommendations.ts`, `monitoring-series.ts`, `approval-chain.ts`) | Needs an I7 backend: recommendation engine, tables and endpoints. **Decision D11**. Until then these stay fixture-backed and get a visible "demo data" banner |
| `/approvals` | **FIXTURE** (I7 approval chain + `lib/approvals.ts`) | Same dependency (D11) |

#### Cross-app screens

| Route | Source today | Target source |
|---|---|---|
| `/home` | **FIXTURE** (`lib/aggregation.ts` over every initiative's fixtures) | New `GET /api/home`: I13 `i13_summary_snapshot` + open ACT counts, I8 snapshot counts, recent `assistant_session`s. I7 tiles stay demo until D11 |
| `/actions` | **FIXTURE** `getAllGlobalActions()` | New `GET /api/actions`: open `i13_act_exception` rows owned by the actor + I8 outstanding declarations (`/i8/declarations?outstandingOnly`) |
| `/audit` | **FIXTURE** `getAllAuditEvents()` | New `GET /api/audit`: `UNION ALL` of the append-only tables `assistant_turn`, `justification`, `consumption_plan`, `i13_act_exception_event`, `i13_act_confirmation`, `i8_attestation`, ordered by time and paged in SQL |
| `/materials`, `/materials/[id]` | **FIXTURE** (`lib/mock-data.ts`, `material-catalog.ts`) | `/i8/universe` already serves material master data from the snapshot (`v_mara`, `v_makt`, `v_marc`, `v_mard`). Extend it with I13 scope (OAR / 80-series) from `raw_marc` and the WATCH mart row, or add `GET /api/materials` over the same views |

### 2.2 Backend route → tables or calculation

Paths are under `/api`. Pagination codes: **SQL** = applied in the query; **slice** = full result built, then `rows[offset:offset+limit]`; **none** = unbounded. No I13 route returns a true total.

**I13 read routes that compute live (the slow ones)**

| Route | Handler | Calculation | Raw tables read | Paging |
|---|---|---|---|---|
| `GET /i13/summary` | `api/i13/routes.py:78` → `initiatives/i13/summary.py:38` | `compute_all_movement_metrics` + `build_exception_queue` (→ `compute_watch_metrics` + `build_reservation_ledger` + `build_procurement_chain`) + `build_reclassification_candidates`, all in Python. **Plans from CSV only** (`exceptions.py:207` omits `db`) | `raw_marc`, `mseg`, `mkpf`, `mard`, `eban`, `ekpo`, `ekbe`, `resb`, `zmm065_*` | none (counts) |
| `GET /i13/validation` | `api/i13/validation.py:35` | Full `build_procurement_chain` + full `build_exception_queue`, then `len()` | same as above | counts |
| `GET /i13/ledger`, `/i13/ledger/{id}` | `api/i13/ledger.py:38,61` | `build_legacy_ledger` (`ledger_compat.py:199`). `{id}` builds the **whole tenant**, then searches linearly | `raw_eban`, `ekpo`, `ekbe`, `mseg`/`mkpf`, `resb`, `marc` | slice |
| `GET /i13/watch` | `api/i13/watch.py:21` | `compute_watch_metrics` (movement metrics + `raw_mard` + reservation ledger + chain + plans CSV **and** `consumption_plan`) | all of the above | none |
| `GET /i13/exceptions` | `api/i13/exceptions.py:21` | `build_exception_queue`, ephemeral (not the ACT table). Plans from CSV only | all of the above | none |
| `GET /i13/reclassification` | `api/i13/reclassification.py:16` | `build_reclassification_candidates`. **Ignores the `i13_reclassification` mart** | `raw_mseg`/`mkpf`, `mard`, `marc`, `zmm065_*` | none |
| `GET /i13/movement-metrics`, `/i13/materials/{m}/plants/{p}/movement-metrics` | `api/i13/movement_metrics.py:21,45` | `compute_all_movement_metrics` | `raw_mseg`/`mkpf`, `raw_mard` | slice |
| `GET /i13/utilisation-ledger/partial`, `…/diagnostics` | `api/i13/procurement_chain.py:20,45` | `build_procurement_chain`. `raw_ekbe` is read **unfiltered** even for one material | `raw_eban`, `ekpo`, `ekbe`, `mseg`/`mkpf` | slice |
| `GET /i13/utilisation-ledger`, `…/{res}/{item}` | `api/i13/reservation_ledger.py:31,67` | `build_reservation_ledger` | `raw_resb` + chain + GI | slice |
| `GET /i13/consumption-attribution`, `…/{res}/{item}` | `api/i13/consumption_attribution.py:67,94` | Reservation ledger + attribution service. **Ignores the `i13_consumption_attribution` mart** | as above + CSV/`consumption_plan` | slice |
| `GET /i13/quantity-suggestion/compute` | `api/i13/assistant.py:282` | Live `compute_watch_metrics` for one material/plant, then `quantity.suggest` | as above | — |

**I13 routes that read tables (fast)**

| Route | Table(s) | Paging |
|---|---|---|
| `GET /i13/act/utilisation`, `…/{material}/{plant}` | `i13_watch_metric_mart` | slice (default 1000, max 5000) / PK |
| `GET /i13/act/exceptions` | `i13_act_exception` (filters in SQL) | **slice** after reading every matching row |
| `GET /i13/act/exceptions/{id}` | `i13_act_exception` + `i13_act_confirmation` + **live** cross-plant `raw_mard` | — |
| `GET /i13/act/exceptions/{id}/history` | `i13_act_exception_event` | none |
| `GET /i13/consumption-plans` | `consumption_plan` | limit in SQL, no offset |
| `GET /i13/quantity-suggestion`, `…/{id}` | `i13_quantity_suggestion` (+ `i13_quantity_justification`) | SQL |
| `GET /i13/data-sources` | `ingestion_run` | — |

**Write routes**

| Route | Reads | Writes |
|---|---|---|
| `POST /i13/act/run/detect` | Live ledger, plans, `i13_watch_metric_mart`, `i13_quantity_suggestion`/`justification`, `raw_resb` | upsert `i13_act_exception`, `i13_act_exception_event`, `i13_act_notification` |
| `POST /i13/act/run/escalate` | `i13_act_exception` | `i13_act_exception`, events, notifications |
| `POST /i13/act/exceptions/{id}/confirmation` | — | `i13_act_confirmation`, `i13_act_exception_event`, update `i13_act_exception` |
| `POST /i13/consumption-plans` | `assistant_session` | `consumption_plan` |
| `POST /i13/quantity-suggestion` (+ `/justification`, `/acceptance`) | `i13_watch_metric_mart` (404 without a row) | `i13_quantity_suggestion`, `i13_quantity_justification` |

**Assistant and justifications**

| Route | Source | Writes |
|---|---|---|
| `POST /assistant/sessions` | I13: **live** `compute_watch_metrics` + `raw_mard`. I8: snapshot | `assistant_session` |
| `POST /assistant/sessions/{id}/turns` | **Rebuilds the live assessment every turn** (`turns.py:323-334`) | `assistant_turn`, `consumption_plan`, `quantity_suggestion`, `justification` (§3.1) |
| `GET /assistant/sessions`, `…/{id}` | `assistant_session` (+ N+1 `assistant_turn`), `consumption_plan`, `quantity_suggestion`, `justification` | — |
| `POST /assistant/ask` | `GROUP BY`/`COUNT` on `i13_act_exception`; I8 snapshot | — |
| `GET` / `POST /justifications` | `justification` (limit in SQL, `total` = page length) | `justification` |

**I8** (`api/i8/router.py`): `/universe`, `/register`, `/declarations`, `/exceptions`, `/vendors/turnaround`, `/repairable-unit` and `/snapshot` read the in-memory snapshot. That snapshot is built once per process from the `v_*` views, which aggregate with SQL `GROUP BY`. Paging is a Python slice, and these routes **do** return true totals. `/attestations` reads and writes `i8_attestation`. `/coding-candidates` screens `v_ekpo` text, calls the LLM when `screen=true`, and caches per process.

**Marts and their refresh:**

| Mart | Rows | Refreshed by | Read by |
|---|---|---|---|
| `i13_watch_metric_mart` | 7,184 | `refresh_watch_metrics_mart`, **called only from tests**. Built with no `db`, so captured plans are excluded | `/act/utilisation`, quantity suggestion, detection |
| `i13_reclassification` | 44,394 | tests only | **nothing** |
| `i13_consumption_attribution` | 41,161 | tests only | **nothing** |
| No mart exists for summary, ledger, procurement chain, monthly consumption or redeployment | | | |

---

## 3. The reservation assistant and the dashboards

### 3.1 What a completed session writes today

**No SAP reservation is created anywhere.** The assistant records a *capture*: `consumption_plan.reservation_number` is always NULL, nothing writes `raw_resb`, and the code says so (`app/api/assistant/router.py:13-20`). The link back to the real reservation waits on RESB.BEDNR (blocker B2).

| Flow / path | Rows written |
|---|---|
| Any start (flow ≠ none) | 1 `assistant_session` (assessment JSON + narrative) |
| Every answer | 1 `assistant_turn`, plus a terminal turn when the next step is terminal (this is what makes the session COMPLETED) |
| I13 → proceed → plan captured | + `consumption_plan` (status OPEN, no reservation number) + `quantity_suggestion` (accepted = requested) |
| I13 → quantity override accepted | + `consumption_plan` + `quantity_suggestion` |
| I13 → override kept → justified | + `consumption_plan` + `quantity_suggestion` + `justification` (kind QUANTITY_OVERRIDE) |
| I08 → proceed with new acquisition | + `justification` (kind NEW_ACQUISITION) |
| I08 with no existing repair | Nothing beyond the session. **Bug:** no terminal turn is written, so the session shows ABANDONED after 72 h instead of COMPLETED |

**Never written by the assistant:**
- `i13_quantity_suggestion` / `i13_quantity_justification`. These form a separate W7.4 store that ACT detection reads.
- `i13_act_*`.
- `i8_attestation`.
- `i13_watch_metric_mart`.
- Any I7 table.

### 3.2 Which pages change after a reservation through the assistant

The frontend never caches, so "Yes" means on the next navigation. After completion the assistant UI does **not** refresh or link anywhere. It shows "This conversation is finished".

| Page | Shows the new session / plan / justification? | Why |
|---|---|---|
| `/assistant/sessions`, `/assistant/sessions/[id]` | **Yes** | Reads `assistant_session`, `assistant_turn` live |
| Session chips on `/oar-utilization/watch` and `/aging-exceptions` | **Yes** | Join `assistant_session` on material + plant |
| `/oar-utilization/plans` | **Yes** | Reads `consumption_plan` |
| Utilisation Dashboard → captured plans, justification log | **Yes** (once the page finishes loading) | `consumption_plan`, `justification` |
| `/repairable-spares/justifications` | **Yes**, NEW_ACQUISITION only | `justification` |
| `/oar-utilization` Overview KPIs | **No** | `/summary` reads plans from CSV only |
| `/oar-utilization/watch` and Dashboard acquired-vs-plan | **No** | The mart is never refreshed, and excludes captured plans even when it is |
| `/oar-utilization/aging-exceptions` (NO_PLAN, PLAN_BREACH, QUANTITY_OVERRIDE) | **No**, not until someone runs detection manually, and even then it is wrong (gaps 3–5) | No scheduler; see §3.3 |
| `/repairable-spares/declarations`, I8 overview, duplicate guard | **No** | Attestations / fixtures; the assistant writes neither |
| `/inventory-planning/*`, `/approvals`, `/home`, `/actions`, `/audit`, `/materials` | **No** | Fixtures |

### 3.3 Gaps that stop the assistant reaching the dashboards

| # | Gap | Where |
|---|---|---|
| G1 | The WATCH mart is never refreshed by the app, and `refresh_watch_metrics_mart` calls `compute_watch_metrics` **without `db`**, so captured plans are excluded | `initiatives/i13/watch_mart.py:116-126` |
| G2 | `/summary`, the legacy `/i13/exceptions` and the attribution mart read plans from **CSV only** | `initiatives/i13/exceptions.py:207`, `consumption_attribution_mart.py:97` |
| G3 | A captured plan can **never clear NO_PLAN**: ACT matches plans on (reservation_number, item), and captured plans have `""` there | `initiatives/i13/plans.py:143-144`, `act/service.py:380-382` |
| G4 | **False PLAN_BREACH**: a captured plan with `window_start` breaches once the grace period passes, because it has no ledger entries and so can never resolve. It measures from `window_start`, although the docstring says window end | `act/service.py:384-386`, `act/detection.py:57-62`, `plans.py:154` |
| G5 | **QUANTITY_OVERRIDE never fires from the assistant**: detection reads `i13_quantity_suggestion`, but the assistant writes `quantity_suggestion` | `quantity_suggestion_store.py:320-333`, `act.py:257` |
| G6 | ACT exceptions change only on a manual `POST /i13/act/run/detect`. The frontend's `runDetectionAction` exists but nothing calls it | `features/initiative-13/actions.ts:107` |
| G7 | The frontend does not refresh or deep-link after completion | `components/assistant/assistant-workspace.tsx:91-92,209-214` |
| G8 | The `/assistant/sessions?material&plant` filter is ignored | `app/assistant/sessions/page.tsx` |
| G9 | I08 sessions without an existing repair end ABANDONED | `assistant/session.py:354-361`, `turns.py:121-124` |
| G10 | The assessment is recomputed live on **every turn** (slow, and the facts can shift mid-conversation) | `assistant/turns.py:323-334` |

### 3.4 Target: one "capture recorded" hook that updates everything downstream

When a session reaches a terminal step, `turns.answer` commits the capture rows and then runs a **scoped post-capture refresh for that (material, plant)**, in the same request (it is cheap once scoped) or as a background task:

1. **Recompute that (material, plant) key in the in-memory snapshot** (`refresh_key`, §4.4) with `db`, so captured plans count (fixes G1 for this part). Swap it in under the snapshot lock. This works today: a single-key `compute_watch_metrics` is what `POST /assistant/sessions` already runs, in under a second.
2. **Run ACT detection scoped to (material, plant)** (`POST /i13/act/run/detect` already takes filters). Before this is switched on, G3 and G4 must be fixed:
   - A captured plan matches on (session → material, plant, window), not on a reservation number, until BEDNR exists (B2).
   - A plan breach is measured from window **end**, and only against issues that exist.
3. **Record the quantity decision in `i13_quantity_suggestion`** (fixes G5). Either write both tables, or preferably retire the assistant's `quantity_suggestion` table and make `i13_quantity_suggestion` the only store. **Decision D12.**
4. **Summary counts that depend on plans or exceptions** are read live from `consumption_plan` / `i13_act_exception` at request time (§4.2), so the Overview reflects the capture without a snapshot refresh (fixes G2 for the KPIs).
5. **Frontend** (fixes G7 and G8):
   - Completion goes through a Server Action that calls `revalidateTag("i13-live")` (plans, exceptions, sessions, WATCH row).
   - The terminal step shows links to **Consumption Plans** (filtered by material), **WATCH** (filtered by material and plant), **Exceptions** (filtered) and the **session trace**.
   - Fix the sessions filter.

**After this, a reservation through the assistant updates these pages:**

| Page | What changes |
|---|---|
| Consumption Plans | New plan row |
| WATCH, Dashboard WATCH | Acquired-vs-plan and plan status for that material/plant |
| Exceptions | NO_PLAN resolved; QUANTITY_OVERRIDE raised when the override was kept |
| Overview, Dashboard | KPI counts (no-plan, overrides, open exceptions) |
| Dashboard justification log, I8 Justifications | The new justification |
| Sessions list and trace | The completed session |
| `/home`, `/actions`, `/audit` | Once §2.1's new endpoints exist: recent sessions, owner actions, audit events |

I8 declarations and I7 are not affected: the assistant does not attest, and I7 has no backend.

---

## 4. Backend work

### 4.1 Serving strategy: the I8 pattern, applied to I13

I8 already meets the one-second goal:
- `app/initiatives/i8/service.py` builds a `Snapshot` once per process (`build_snapshot`, 3.0 s measured).
- It keeps the snapshot in a lock-guarded global (`get_snapshot`) and answers every register, declaration and universe read from memory (0.25 s).
- It reads only user-written data (`i8_attestation`) live.

I13 does the same:

```
 backend start ──► background thread: build_i13_snapshot(db)   (one pass over raw_*, minutes; measured in step 1)
                        │   reuses the existing Python builders, sharing ONE repository instance,
                        │   so every raw_* table is read once for the whole build
                        ▼
              I13Snapshot (immutable, in memory, atomically swapped)
                        │
   GET /api/i13/*  ──►  filter + page the in-memory objects   (< 100 ms)
                        │   + overlay live tables: consumption_plan, i13_act_exception,
                        │     assistant_session, justification, i13_quantity_suggestion
                        ▼
   assistant capture ──► refresh_key(material, plant) ──► swap one entry (§3.4)
```

**Rules:**
1. After warm-up, no request computes from `raw_*`. It reads the snapshot. The only exceptions are the single-key assistant paths, which are already fast.
2. Data users write is never frozen in the snapshot. It is read from its table on each request, as I8 does with attestations.
3. The build never blocks the server:
   - During the first build, snapshot-backed endpoints answer **503 with `Retry-After`** and a `building` status, and the frontend shows "Preparing data".
   - During a rebuild, the **old snapshot keeps serving** until the new one is swapped in.
4. Every response carries the snapshot's `built_at` and reference date (headers, §4.8).

### 4.2 What the snapshot holds

Each rev 2 mart becomes a field of `I13Snapshot`. The builders already exist, except for the three new screens.

| Snapshot component | Replaces rev 2 table | Built by (existing code unless marked new) | Feeds |
|---|---|---|---|
| `summary` (per plant + ALL) | `i13_summary_snapshot` | `summary.build_summary`, split per plant | Overview, Dashboard KPIs, `/home` |
| `movement_metrics` | — | `compute_all_movement_metrics` | `/movement-metrics*` |
| `watch` (dict keyed by material + plant, full population, **built with `db`**) | `i13_watch_metric_mart` | `compute_watch_metrics(..., db=db)` | WATCH (`/act/utilisation`, `/watch`), Dashboard, Overview, quantity suggestion, detection |
| `procurement_chain` | `i13_procurement_chain_mart` | `build_procurement_chain` | Validation, `/utilisation-ledger/partial*` |
| `reservation_ledger` + `legacy_ledger` | `i13_utilisation_ledger_mart` | `build_reservation_ledger`, `ledger_compat.build_legacy_ledger` | Ledger, `/utilisation-ledger*`, `/ledger/{id}` (dict lookup instead of a full-tenant rebuild) |
| `consumption_attribution` | `i13_consumption_attribution` | `ConsumptionAttributionService` (with `db`, G2) | `/consumption-attribution*` |
| `reclassification` | `i13_reclassification` | `build_reclassification_candidates` | Reclassification, Dashboard |
| `validation_counts` | — | counts over `procurement_chain` + the exception queue | `/validation` |
| `monthly_consumption` | `i13_monthly_consumption` | **new**: group the already-loaded MSEG rows by (material, plant, month) | Usage Pattern, Overview charts, FR-3, assistant |
| `grni` | (ledger mart rows) | **new**: filter `reservation_ledger` with the FR-6 rule | 30-Day GRNI |
| `redeployment` | `i13_redeployment_candidate` | **new**: `watch` + cross-plant MARD; rule needs VZI (D8) | Redeployment |
| `cross_plant_stock` | — | `raw_mard` by material | Exception detail (today a live query per call), assistant |

**Read live on every request (not in the snapshot):**

| Table | Why | Paging |
|---|---|---|
| `i13_act_exception` (+ events, confirmations) | Changes on confirmation and detection | SQL `limit`/`offset` + `COUNT` (§4.8) |
| `consumption_plan` | Changes on every assistant capture | small; SQL |
| `assistant_session`, `assistant_turn`, `justification` | User writes | SQL, one joined query |
| `i13_quantity_suggestion` / `i13_quantity_justification` | User writes | SQL |

**Summary counts that depend on these tables** (no-plan, plan breaches, overrides, open exceptions by status) are computed at request time as `COUNT`s over them and merged into `summary`. The Overview therefore reflects a capture without a rebuild.

### 4.3 Where it lives in the code

| Piece | Location | Mirrors in I8 |
|---|---|---|
| `I13Snapshot` frozen dataclass + `build_i13_snapshot(db, cfg)` | new `app/initiatives/i13/snapshot.py` | `i8/service.py:56-126` |
| `get_i13_snapshot()`, `reset_i13_snapshot()`, lock-guarded global, `status` (`building` / `ready` / `failed`, `built_at`, `build_seconds`, `fingerprint`) | same file | `i8/service.py:129-158` |
| `refresh_key(material, plant)`: recompute one WATCH / attribution / monthly entry and swap it into a copied dict | same file | (new; I8 has no equivalent) |
| A FastAPI dependency `I13SnapshotDep` that returns the snapshot or raises 503 while it is building | `app/api/i13/deps.py` | — |
| Each `/api/i13/*` read route: `snapshot.<component>` + filter + page instead of calling the builder | `app/api/i13/*.py` | `app/api/i8/router.py` |
| `?live=true` keeps calling the builder directly, for parity tests | same routes | — |

**The existing `i13_watch_metric_mart`** is still read by `POST /i13/quantity-suggestion` and `POST /i13/act/run/detect`. Both switch to `snapshot.watch`. The mart table stays in the schema, unused, until Phase B.

### 4.4 Build, refresh and warm-up

| Event | What happens |
|---|---|
| **Backend start** | FastAPI `lifespan` starts the I13 build (and warms the I8 snapshot) in a **background thread**. The server accepts requests immediately |
| **First build running** | Snapshot-backed routes → 503 `{"status":"building","startedAt":…}` + `Retry-After: 10`. Live-table routes (plans, exceptions, sessions, justifications) work normally |
| **Reseed** (`python -m app.seed`) | The snapshot fingerprint (latest `ingestion_run` ids + I13 config hash) changes. A **cheap check** (one `ingestion_run` query, 0.25 s measured) runs at most every 60 s and starts a background rebuild; the old snapshot keeps serving. The same check resets I8 |
| **Manual** | `POST /api/run/refresh-snapshots` (I8 + I13), returns 202. Optional "Refresh data" button |
| **Assistant capture / plan write** | `refresh_key(material, plant)` only (§3.4) |
| **Build fails** | Status `failed` with the error. The previous snapshot (if any) keeps serving, and `/api/ready` reports it |
| **Reference date** | Fixed per snapshot: the latest MKPF posting date (D1), so results don't drift day to day, the same as I8's `reference_date` |

**The I8 snapshot gets the same treatment:**
- The fingerprint check.
- `POST /api/run/refresh-snapshots`.
- Startup warm-up.

It currently never refreshes: `get_snapshot(refresh=True)` and `reset_snapshot()` have no callers outside tests, so a reseed needs a backend restart.

**Deployment constraint:** each worker process holds its own snapshot and pays its own build. **Run the API with one worker** for now (D14).

### 4.5 Making the build itself shorter (optional, not blocking)

The build runs once per process, not per request, so it may take minutes. These fixes shorten it, but Phase A does not wait on them:
- Scope the EKBE, GI-candidate and GI-by-reservation reads (they ignore material/plant filters today).
- Share one memoising repository instance across all builders, so `raw_mseg` (233k rows) and the rest load once.
- Move the heaviest aggregations into SQL `GROUP BY`.
- Port `app/refresh_marts` and `app/integrations/sap/sql_filters.py` from `feat/sj/init13`. Only `.pyc` files exist on this branch, and `sql_filters` holds the scoped query filters.

### 4.6 I8 views

Create the 11 `v_*` views in an Alembic migration (or at startup and in `app.seed`), so a fresh DB never 500s on I8 again.

### 4.7 Phase B (later, only if needed): persist the snapshot to Postgres

Move to the rev 2 mart tables (`i13_summary_snapshot`, `i13_utilisation_ledger_mart`, `i13_procurement_chain_mart`, `i13_monthly_consumption`, `i13_redeployment_candidate`, the full `i13_watch_metric_mart`) when any of these becomes true:

| Trigger | Why the in-memory pattern stops fitting |
|---|---|
| More than one API worker or instance is needed | Each builds and holds its own copy, and the copies can briefly disagree |
| The startup build becomes too slow for restarts or deploys | Every restart pays it; persisted marts survive restarts |
| Snapshot memory is too large for the host | Measured in step 1 |
| Other consumers (reports, Power BI) need the same numbers | They can read tables, not process memory |

In Phase B the snapshot builders write tables instead of dataclasses, and the routes switch from `snapshot.x` to one indexed query. The API contract (paging, headers) stays the same, so the frontend doesn't change.

### 4.8 API changes

- **Paging on every list route:**
  - `limit` / `offset` with default 100 and max 1,000, plus an **`X-Total-Count` header**. Bare-array bodies are unchanged (D7).
  - Snapshot-backed routes page the in-memory list; slicing is fine there because the work is already done.
  - Live-table routes (`/act/exceptions`, sessions, justifications) page **in SQL**. Today `/act/exceptions` reads all 42,649 rows, then slices.
- **Snapshot headers**: `X-I13-Snapshot-Built-At`, `X-I13-Data-As-Of`, `X-I13-Snapshot-Status` (`ready` / `rebuilding`). **`?live=true`** kept for parity tests.
- **`GET /api/run/snapshots`**: status of the I8 and I13 snapshots (building / ready / failed, `built_at`, `build_seconds`, fingerprint). **`POST /api/run/refresh-snapshots`** rebuilds both.
- **New endpoints:**
  - `/i13/act/confirmations`, which replaces the 1 + 2 + 30-call fan-out.
  - `/i13/grni`, `/i13/usage-patterns`, `/i13/redeployment`.
  - `/i8/overview`.
  - `/home`, `/actions`, `/audit`, `/materials` (§2.1).
- **`/i13/summary?plant=`**.
- **Assistant:**
  - The session list becomes one joined query with a true total.
  - The assessment is built from `snapshot.watch` + `snapshot.monthly_consumption` once per session and stored; turns reuse it (G10).
  - Fix G9.

| Endpoint | Before (measured) | After (Phase A, snapshot warm) |
|---|---|---|
| `/i13/summary` | > 90 s | `snapshot.summary` + live counts, < 50 ms |
| `/i13/reclassification` | > 90 s | `snapshot.reclassification`, paged, < 100 ms |
| `/i13/validation` | 16.9 s | `snapshot.validation_counts`, < 50 ms |
| `/i13/watch` | 11.8 s, 12.9 MB | `snapshot.watch`, paged, < 100 ms |
| `/i13/ledger`, `/ledger/{id}` | 9.2 s / full-tenant rebuild | `snapshot.legacy_ledger`, paged / dict lookup, < 100 ms |
| `/act/exceptions` | 1.24 s, reads 42,649 rows | live table, paged in SQL, < 100 ms |
| `/act/utilisation` | 0.38 s, 1 MB, stale partial mart | `snapshot.watch`, full population, paged, < 100 ms |
| `POST /assistant/sessions` (I13), each turn | live compute every turn | snapshot entry + monthly consumption, once per session, < 300 ms |
| `/i8/*` first call after restart | 3 s | warmed at startup; refreshed on reseed |
| Any snapshot route during the first build | — | 503 `building` + `Retry-After` (seconds, not minutes, to answer) |

### 4.9 Remaining fixes

- **Plan matching** for captured plans (G3) and plan-breach timing (G4), before detection is triggered from the assistant.
- **30-Day GRNI rule:** received − issued > 0 and oldest outstanding GR ≥ 30 days (FRS FR-6). No sign-off needed.
- **Usage Pattern:** monthly net issues over the ~12.5 months available. Label the history depth (B4).
- **Redeployment draft rule:**
  - Stock at plant A that is NON_MOVING/SLOW or above the cover ceiling, **and** consumption or open demand at plant B.
  - Advisory only (D10).
  - Plant 1500 has no MARC (B3).
  - **Needs VZI (D8).**

---

## 5. Frontend work

| Change | Where | Effect |
|---|---|---|
| **`loading.tsx`** for `/oar-utilization`, `/repairable-spares`, `/assistant`, `/inventory-planning` and the root | `src/app/**/loading.tsx` | A nav click switches screen immediately |
| **`<Suspense>` per section** instead of `Promise.all` | Overview, Utilisation Dashboard (`live-dashboard.ts`), `/assistant/sessions`, I8 declarations | The fastest section appears first; one failure doesn't blank the page |
| **`apiFetch` timeout** (`AbortSignal.timeout`, 15 s for GETs) | `src/lib/api/client.ts` | Clear "backend did not respond" instead of a multi-minute hang |
| **"Database unavailable"** error state (optional `/api/ready` probe) | loaders | A DB outage is not mistaken for an empty screen |
| **Server-side paging** with `X-Total-Count` | WATCH, Exceptions, Ledger, Reclassification, I8 register and declarations (stop fetching every page sequentially) | 100 rows + total; drop `ROW_CAP` / `atLimit` |
| **"Preparing data" state** for a 503 `building` response: show the skeleton + "Preparing data (started {time})" and retry after `Retry-After` | shared loader error handling | A backend restart reads as "warming up", not "broken" |
| **Cache tags (optional in Phase A)**: the backend already answers from memory in < 100 ms, so the frontend cache only saves the network hop | `lib/api/i13.ts`, `lib/api/i8.ts` | If added: `i13-snapshot` (invalidated when `X-I13-Snapshot-Built-At` changes) and `i13-live` (invalidated by assistant completion and exception confirmation) |
| **Assistant completion** Server Action + deep links on the terminal step | `components/assistant/assistant-workspace.tsx`, `step-renderer.tsx` | §3.4 step 5 |
| **Sessions filter** reads `searchParams` | `app/assistant/sessions/page.tsx` | G8 |
| **Wire `runDetectionAction`** to an admin "Run detection" button, until §3.4 makes it automatic | `features/initiative-13` | G6 |
| **Replace fixtures with live loaders** | Redeployment, GRNI, Usage Pattern, Overview charts, I8 overview, duplicate guard, `/home`, `/actions`, `/audit`, `/materials` | Every page from Postgres |
| **Visible "demo data" banner** on anything still fixture-backed (I7, approvals) | I7 pages, I8 overview (the banner was removed in `fba5ad3`) | No fixture screen passes for live data |
| **"Data as of … · refreshed …"** + stale banner | shared page header | One freshness indicator for every screen |

---

## 6. Target times (`next build && next start`, snapshot warm)

The I13 snapshot build time is **not yet known**. Step 1 measures it, along with its memory footprint. Target: < 5 min for the build and < 2 GB for the process.

| Screen | Skeleton | Data complete |
|---|---|---|
| Every nav screen | < 200 ms | < 1 s |
| Overview / Dashboard | < 200 ms | first sections < 500 ms, all < 1 s |
| Exceptions (page of 100 of 42,649) | < 200 ms | < 500 ms |
| Assistant: start session / each turn | < 200 ms | < 300 ms |
| Dashboards after an assistant capture | — | reflect it on the next view (no manual refresh or detection run) |
| Second visit (cached) | — | < 200 ms |

---

## 7. Order of work

| Step | What | Size | Depends on | Result |
|---|---|---|---|---|
| **0** | Local environment: DB up, `alembic upgrade head`, I8 views created (**done on 24-Sep**, §0). Put the I8 views into Alembic/seed (§4.6) | S | — | Every live endpoint answers; a baseline is recorded (§1) |
| **1** | Quick wins: SQL `limit`/`offset` + `X-Total-Count` on `/act/*`; `loading.tsx` everywhere; `apiFetch` timeout; Suspense on Overview/Dashboard/sessions; sessions filter (G8); I08 ABANDONED (G9); demo banners back on fixture pages | S | 0 | Nav is instant; Exceptions/WATCH stop shipping every row |
| **2** | **I13 snapshot core** (§4.1–4.4): `snapshot.py` with `summary`, `movement_metrics`, `watch` (with `db`), `reclassification`, `procurement_chain`, `reservation_ledger`/`legacy_ledger`, `validation_counts`, `consumption_attribution`, `cross_plant_stock`. Background build at startup, 503 `building`, fingerprint check, `POST /api/run/refresh-snapshots`. **I8 gets the same warm-up + refresh.** Switch every I13 read route, plus detection and quantity suggestion, to the snapshot. **Measure the build time and memory** | M | 1, D1 | Every I13 screen loads in < 1 s once warm; a reseed shows up without a restart |
| **3** | **Assistant → dashboards**: fix G3/G4; single-store quantity decisions (G5, D12); `refresh_key` + scoped detection after a capture (§3.4); assessment from the snapshot once per session (G10); frontend completion revalidation + deep links | M | 2 | A reservation through the assistant updates Plans, WATCH, Exceptions and the KPIs on the next view |
| **4** | New snapshot components `monthly_consumption` and `grni`; GRNI, Usage Pattern and Overview charts go live; `/act/confirmations` | M | 2 | Three fixture surfaces become real; the justifications fan-out is gone |
| **5** | Frontend server-side paging (I13 and I8), "Preparing data" state, "Data as of" header (cache tags optional) | M | 2 | Totals shown; restarts read as warm-up |
| **6** | Cross-app endpoints `/home`, `/actions`, `/audit`, `/materials`, `/i8/overview` (built on both snapshots + live tables); duplicate guard on the I8 snapshot; persisted coding screen | M | 2, 3 | Every non-I7 page from Postgres |
| **7** | Redeployment rule sign-off, then the `redeployment` snapshot component and screen | S | 2, VZI | Last I13 fixture gone |
| **8** | FR-3 corrections (previous plan §5) on the snapshot | M | 2, 4 | Quantity suggestion correct and instant |
| **9** | Optional: shorten the build (§4.5) | M | 2 | Faster restarts and refreshes |
| **10** | I7 backend (only if D11 says so) | L | D11 | I7 and approvals live |
| **11** | Parity tests (snapshot vs `?live=true`), refresh/fingerprint tests, the assistant → dashboard end-to-end test, the §6 timings measured | M | all | Proof |
| **Later** | **Phase B**: persist the snapshot components to Postgres mart tables, only if a §4.7 trigger is hit | L | 2 | Multi-worker, restart-proof serving |

Steps 0–3 make the screens usable and connect the assistant. Steps 4–6 make every non-I7 page live. Compared with rev 2, the three separate mart steps (summary, ledger/chain, monthly) collapse into one snapshot step (2), because the builders already exist.

---

## 8. Decisions needed

| ID | Decision | Recommendation |
|---|---|---|
| D1 | Metrics as of the snapshot date or today | **Snapshot date** |
| D6 | During a rebuild: serve the old snapshot or refuse | **Serve the old snapshot**, marked `rebuilding`; never recompute inside a request |
| D7 | Total count: header or body envelope | **Header** (`X-Total-Count`) |
| D8 | Redeployment candidate rule | Needs VZI; draft in §4.9 |
| D9 | Frontend cache lifetime | **Until the snapshot changes** (tag-invalidated); live tables tag-invalidated on writes |
| D11 | Build an I7 backend now, or leave I7 and approvals as labelled demo screens | **Labelled demo for now**; I7 needs its own design |
| D12 | Two quantity-suggestion tables (`quantity_suggestion` vs `i13_quantity_suggestion`) | **Keep only `i13_quantity_suggestion`**; the assistant writes it directly |
| D13 | Post-capture refresh in the request or as a background task | **In the request** once scoped (one snapshot key + scoped detection); background only if measured > 500 ms |
| D14 | API worker count while serving from memory | **One worker** (Phase A). More workers means Phase B |
| D15 | What snapshot routes answer during the first build | **503 `building` + `Retry-After`**, not a slow live compute (which would pin the GIL and slow the build) |

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Snapshot and live results differ | Parity tests against `?live=true`, kept permanently |
| A rebuild fails halfway | The new snapshot is built off to the side and swapped in only when complete; the previous one keeps serving; status `failed` shows in `/api/ready` |
| The startup build takes minutes; screens show "Preparing data" after every restart | Measured in step 2; §4.5 shortens it; Phase B removes it |
| Snapshot memory is too large for the host | Measured in step 2; frozen tuples/dicts, no raw rows kept after the build; Phase B if over budget |
| The build holds the GIL and slows other requests while it runs | Only at startup or after a reseed; live-table routes stay responsive enough; off-hours refresh if needed |
| Someone runs several workers and gets inconsistent answers | D14 documented in `startup.sh` / README; Phase B if scale requires it |
| A reseed is not picked up | Fingerprint check every 60 s + manual refresh; `X-I13-Snapshot-Built-At` visible on every screen |
| Scoped detection after a capture raises false exceptions | G3/G4 fixed and tested **before** it is switched on |
| Paging changes what client-side filters see | Filters move to the server with paging (URL params exist) |
| A fresh DB lacks I8 views or migrations again | Views in Alembic/seed; `/api/ready` checks them; "Database unavailable" state |
| Redeployment rule contested | Stays demo-bannered until signed off |
| I7 pages mistaken for live | Demo banner restored until D11 |

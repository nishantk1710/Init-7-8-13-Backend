# Initiative 13 — Making Every OAR Screen Load Instantly: Plan

| | |
|---|---|
| **Date** | 24 September 2026 |
| **Status** | PLAN ONLY — nothing here is implemented |
| **Backend** | `Init-7-8-13-Backend`, `feat/vp/ws7-assistant` |
| **Frontend** | `Init-7-8-13-Frontend`, `feat/vp/ws7-assistant-frontend` |
| **Builds on** | `I13_Snapshot_Serving_and_FR3_Plan_24_Sep_2026.md`. Its §3 (snapshot fingerprint + refresh) and §5 (FR-3) still apply. **This document replaces its §4 (serving) and §8 (order of work).** |

**Goal:** every screen under `/oar-utilization` shows its layout at once and its data in **under one second**, on the seeded data, with no screen waiting for another.

---

## 1. What was found (checked on 24-Sep, not assumed)

### 1.1 Why nothing loads *right now*: the database is down

| Check | Result |
|---|---|
| `GET /api/health` | 200 in 0.28 s. The API process is up |
| Every data endpoint the nav uses (`/act/utilisation`, `/act/exceptions`, `/ledger`, `/consumption-plans`, `/assistant/sessions`, `/justifications`) | **500 "Internal server error" after ~7.5 s**, including the tiny consumption-plans table |
| Port 5432 on 127.0.0.1 | **Connection refused** |
| Docker | **Daemon not running**, so the `spares-postgres` container from `compose.yaml` is down |

A 500 after ~7.5 s on *every* endpoint, including the cheapest one, is the 5-second `database_connect_timeout_seconds` failing, not slow computation. **No code change fixes this.** Step 0 in §6 does.

> Performance could not be re-measured today with the database down. The per-endpoint timings below come from the 23-Sep measurements in `Initiative_13_Current_State_23_Sep_2026.md` §9. Step 0 includes re-measuring them.

### 1.2 Why screens are slow once the database is up: screen by screen

| Nav screen | What the page waits for | Why that is slow or heavy |
|---|---|---|
| **Overview** | `/i13/summary` **and** `/act/utilisation`, together | `/summary` recomputes everything from the raw tables: **~4 min** on this branch. The page renders only when both return, so the whole screen waits ~4 min |
| **Utilisation Dashboard** | One `Promise.all` over 7 sections: summary, WATCH, reclassification, ACT exceptions, justifications, plans, validation | Each section "degrades independently" for *errors*, but **all seven must finish before anything renders**, so it waits for `/summary` (~4 min). Reclassification and validation also recompute live. Justifications = 1 shared list + 2 ACT exception lists + up to **30 detail calls** |
| **Utilization Ledger** | `/i13/ledger?limit=1000` | Builds the whole PR→PO→GR→GI chain, then slices. `limit` bounds the payload, not the work |
| **Exceptions** | `/act/exceptions?limit=1000` + `/assistant/sessions` | Reads a table (fast query), but **`limit` is not declared on the route, so FastAPI ignores it** and all **42,649** rows are returned, serialised, and passed to a client table |
| **Consumption Plans** | `/i13/consumption-plans` | Small table. Fast |
| **WATCH** | `/act/utilisation?limit=1000` | Reads the mart (0.28 s), but **`limit` is ignored** here too, so all 7,184 rows are shipped to the browser |
| **Reclassification** | `/i13/reclassification` | **Live recompute** on this branch (the mart read path is only on `feat/sj/init13`) |
| **Validation** | `/i13/validation` | Builds the full procurement chain live (255,916 entries) |
| **30-Day GR-Not-Issued**, **Usage Pattern**, **Redeployment** | Nothing: fixture files | No backend call. Slowness here is not a data problem (see §1.3) |

### 1.3 Frontend factors that make it *feel* broken

| Factor | Effect |
|---|---|
| **No `loading.tsx` under `/oar-utilization`** (only `/audit` and `/materials` have one) | Clicking a nav item shows nothing: the old page stays on screen until the server finishes. A 4-minute render looks like a dead link, and so can a click on a static page while another render is still running |
| **All-or-nothing `Promise.all`** on Overview and the Dashboard | The fastest section waits for the slowest |
| **`apiFetch` has no timeout** | A hung or very slow backend holds the page until Node's own ~5-minute header timeout, then fails with a generic "fetch failed" |
| **No caching of GETs** | Every navigation repeats every request, even though the data changes only on a reseed |
| **`next dev`** | Each route compiles on first visit, which adds seconds the first time. Measure with `next build && next start` |

### 1.4 A backend factor that slows fast endpoints too

Heavy endpoints aggregate hundreds of thousands of rows **in Python** (`/summary` pulls all 233k movement rows). FastAPI runs sync routes in a thread pool, but Python-level computation holds the GIL. **While one `/summary` is running, even the 0.28-second mart reads slow down.** The Dashboard fires about 10 requests at once, so it makes this worse for itself. Moving the computation out of the request path (§3) fixes this for every screen at once.

---

## 2. Target design in one picture

```
 python -m app.seed ──► raw_* tables ──► python -m app.refresh_marts  (once per data snapshot)
                                               │
                     ┌─────────────────────────┼──────────────────────────────┐
                     ▼                         ▼                              ▼
          i13_summary_snapshot     i13_watch_metric_mart (full)     i13_utilisation_ledger_mart
          i13_reclassification     i13_monthly_consumption          i13_procurement_chain_mart
          i13_redeployment_candidate                                i13_act_exception (exists)
                     │
                     ▼
   every GET /api/i13/*  =  one indexed SQL query, filtered + paged in SQL   (< 100 ms)
                     │
                     ▼
   Next.js: loading.tsx instantly → sections stream in via <Suspense> → cached until snapshot changes
```

**Rules:**
1. No nav-screen request computes anything from `raw_*` tables. It reads a precomputed table.
2. Every list endpoint filters and pages **in SQL** and reports the total count.
3. Every response says which snapshot it came from and whether that snapshot is stale.
4. Things that change without a reseed (captured plans, exception confirmations, assistant sessions) are small tables read directly, or applied at read time (plan overlay, previous plan §3.5).

---

## 3. Backend work

### 3.1 Snapshot and refresh (from the previous plan, §3, unchanged)

Snapshot fingerprint from `ingestion_run`; config fingerprint; `i13_mart_refresh_run` table; `python -m app.refresh_marts` (ported from `feat/sj/init13`); run automatically at the end of `app.seed`; `POST /api/i13/run/refresh-marts` for a manual button; data measured "as of" the latest MKPF posting date (decision D1).

### 3.2 Precomputed tables: one per nav need

| Table | New? | Feeds | Contents |
|---|---|---|---|
| `i13_summary_snapshot` | **New** | Overview, Dashboard KPIs | **One row per plant plus one "ALL" row**: every `/summary` count (OAR positions, fast/slow/non-moving, GRNI-30d, no-plan, reclassification candidates, and so on). `/summary` becomes a primary-key lookup. Counts that change without a reseed (plan breaches, no-plan, exception statuses) are read as a live `COUNT` over `i13_act_exception`, which is cheap and indexed |
| `i13_watch_metric_mart` | Exists | WATCH, Dashboard, Overview charts, FR-3, assistant | Refresh the **full population** (both plants, `oar_only=False`; today it holds 7,184 rows, 16% of OAR). Add indexes on `plant`, `aging_band`, `gr_not_issued_flag`, `material_scope` |
| `i13_reclassification` | Exists | Reclassification, Dashboard | Serve from it (port the read path from `feat/sj/init13`) |
| `i13_utilisation_ledger_mart` | **New** | Ledger, `/utilisation-ledger*`, GRNI screen | One row per W6.2 ledger entry with lifecycle and linkage status; `ledger_compat.py` maps from it |
| `i13_procurement_chain_mart` | **New** | Validation, `/utilisation-ledger/partial*` | One row per W6.1 entry; validation counts become `GROUP BY` queries |
| `i13_monthly_consumption` | **New** | Usage Pattern screen, FR-3 look-back | `(material, plant, month)`: net issued quantity, issue count, receipts |
| `i13_consumption_attribution` | Exists, unused | `/consumption-attribution`, the ledger's attribution fields | Serve from it |
| `i13_redeployment_candidate` | **New** | Redeployment screen | See §3.5. Needs a business-rule sign-off first |
| `i13_act_exception` | Exists | Exceptions, Dashboard | Add paging and indexes (`status`, `exception_type`, `plant`, `owner_requester_id`) |

### 3.3 API changes (the same for every list endpoint)

- **`limit` / `offset` declared and applied in SQL** on all I13 list routes, including `/act/utilisation` and `/act/exceptions`, where they are ignored today. Default 100, maximum 1,000.
- **Total count** in an `X-Total-Count` header. Bare-array bodies stay, so no response shape changes for existing callers; the frontend then shows "1–100 of 42,649" instead of an `atLimit` guess.
- **Snapshot headers**: `X-I13-Snapshot-Id`, `X-I13-Data-As-Of`, `X-I13-Refreshed-At`, `X-I13-Stale`.
- **`?live=true`** kept on every mart-backed endpoint, for comparing against a live recompute (the previous plan's parity tests).
- **New `GET /i13/act/confirmations`** (plant, material, limit, offset): one query listing confirmations with their exception. This replaces the frontend's 1 + 2 + 30-call justification fan-out.
- **`/i13/summary?plant=`**: now possible, because the snapshot table has per-plant rows.

| Endpoint | Before | After |
|---|---|---|
| `/i13/summary` | live, ~4 min | snapshot row + ACT counts, target < 50 ms |
| `/i13/watch`, `/act/utilisation` | live 12 s / mart but unpaged | mart, paged, < 100 ms |
| `/i13/movement-metrics*` | live | WATCH mart |
| `/i13/reclassification` | live | mart, paged |
| `/i13/ledger`, `/utilisation-ledger*` | build all, then slice | ledger marts, paged |
| `/i13/validation` | live chain | `GROUP BY` over the chain mart |
| `/i13/consumption-attribution*` | live | existing mart |
| `/act/exceptions` | all 42,649 rows | paged, < 100 ms |
| `/i13/quantity-suggestion`, `POST /assistant/sessions` (I13) | live WATCH compute | mart row + monthly consumption (previous plan §5) |
| new `/i13/grni` | — (demo screen) | ledger mart rows with received > issued and GR ≥ 30 days |
| new `/i13/usage-patterns` | — (demo screen) | monthly consumption mart |
| new `/i13/redeployment` | — (demo screen) | redeployment candidate mart |

### 3.4 Also fix, so the refresh itself is quick

- Material and plant filters on the EKBE, GI-candidate and GI-by-reservation queries (previous plan §3.6).
- Move the `/summary` movement aggregation into SQL `GROUP BY`. The refresh then takes seconds to a few minutes, not tens of minutes. Record the measured time.

### 3.5 The three demo screens made real

| Screen | Source | Rule | Needs sign-off? |
|---|---|---|---|
| **30-Day GR-Not-Issued** | `i13_utilisation_ledger_mart` | Entries with received − issued > 0 and oldest outstanding GR ≥ `I13_GR_NOT_ISSUED_THRESHOLD_DAYS` (30). This is the rule WATCH already applies (`watch._gr_not_issued`), stored per entry | No: the rule exists (FRS FR-6) |
| **Consumption / Usage Pattern** | `i13_monthly_consumption` | Monthly net issues per material and plant; trend over the available ~12.5 months | No, but label the history depth (blocker B4) |
| **Redeployment** | `i13_redeployment_candidate`, built from the WATCH mart and cross-plant MARD stock | Draft: material has stock at plant A that is NON_MOVING/SLOW **or** above the cover ceiling, **and** consumption or open demand at plant B. Advisory only; no transfer is proposed or executed (D10) | **Yes: VZI must approve the rule.** Until then, keep the demo banner, or show the draft rule with a "proposed rule" banner. Note that plant 1500 has no MARC (B3), so plant-B demand there comes only from MSEG/RESB |

Until the new endpoints exist, each demo screen keeps its fixture and banner. Nothing is removed before its replacement works.

---

## 4. Frontend work

| Change | Where | Effect |
|---|---|---|
| **`loading.tsx`** for the `/oar-utilization` segment (and a skeleton per page where layouts differ) | `src/app/oar-utilization/**/loading.tsx` | A nav click switches screens **immediately** and shows a skeleton |
| **Stream sections with `<Suspense>`** instead of one `Promise.all` | `overview-page.tsx`, `utilisation-dashboard-page.tsx`, `live-dashboard.ts` | Each section appears when its own data arrives. One slow or failed section no longer holds the page. The `Section<T>` ready/unavailable/error rule stays, now applied per section |
| **Timeout on `apiFetch`** (`AbortSignal.timeout`, e.g. 15 s for GETs, configurable) | `src/lib/api/client.ts` | A slow backend shows a clear "backend did not respond in 15 s" error instead of hanging for minutes |
| **Distinguish "backend unreachable / DB down"** | error rendering in the loaders | A 500 with the DB down reads as "Database unavailable", not as an empty or broken screen. Optional: a `/api/health/db` probe |
| **Server-side paging** | WATCH, Exceptions, Ledger, Reclassification tables | Ask for 100 rows plus total; the page and filters live in the URL (the pattern exists already). Drop `ROW_CAP = 1000` and the `atLimit` guess |
| **Cache snapshot-derived GETs** in the Next data cache (`next: { tags: ["i13-snapshot"] }`) | `lib/api/i13.ts` | The second visit to any screen is instant. **Not cached:** plans, sessions, justifications, exceptions (they change on user writes and are already fast) |
| **Invalidate on refresh**: the "Refresh data" Server Action calls `revalidateTag("i13-snapshot")`; a cheap `GET /i13/snapshot` check does the same when the backend's snapshot id differs from the cached one | `features/initiative-13/actions.ts` | A reseed or refresh shows up without a manual reload |
| **Use `/act/confirmations`** in `getI13Justifications` | `lib/api/i13.ts` | One request instead of up to 33 |
| **"Data as of 20-Aug-2026 · refreshed {time}"** and a **stale** banner | shared page header | Replaces the per-screen `calculatedAt` note |
| **Live GRNI / Usage / Redeployment** loaders, once §3.5 lands | `data/live-loaders.ts`, the three pages | Removes the fixtures and banners |

---

## 5. Target times (measured with `next build && next start`, warm marts)

| Screen | Skeleton appears | Data complete |
|---|---|---|
| Every nav screen | < 200 ms after the click | < 1 s |
| Overview / Dashboard | < 200 ms | first sections < 500 ms, all < 1 s |
| Exceptions (page of 100 out of 42,649) | < 200 ms | < 500 ms |
| Second visit to any screen (cached) | — | < 200 ms |
| `python -m app.refresh_marts` (full) | — | measured and recorded; runs once per snapshot, never in a request |

---

## 6. Order of work

| Step | What | Size | Depends on | Result |
|---|---|---|---|---|
| **0** | **Start Docker Desktop, then `docker compose up -d` in `Init-7-8-13-Backend`.** Confirm 5432 is open and `/i13/consumption-plans` returns 200. Then **re-measure** every endpoint in §1.2 and record the numbers here | minutes | — | Screens load again (slowly) and there's a real baseline |
| **1** | Quick wins, no new tables: `limit`/`offset` + `X-Total-Count` on `/act/*`; `loading.tsx`; `apiFetch` timeout; Suspense streaming on Overview and the Dashboard | S | 0 | Nav responds instantly; Exceptions and WATCH stop shipping every row; the Dashboard shows the fast sections at once |
| **2** | Refresh infrastructure (previous plan §3) + `i13_summary_snapshot` + full-population WATCH mart + reclassification mart read path | M | 1, decisions D1/D4 | `/summary`, Overview, Dashboard, WATCH, Reclassification are instant |
| **3** | Ledger and procurement-chain marts; serve Ledger, Validation, Attribution; `/act/confirmations` | L | 2 | Ledger, Validation and the justifications section are instant |
| **4** | Frontend server-side paging and Next caching with snapshot invalidation; "Data as of" header | M | 2, 3 | Instant repeat visits; totals shown |
| **5** | Monthly consumption mart; GRNI and Usage Pattern screens live | M | 3 | Two demo screens become real |
| **6** | Redeployment rule sign-off, then its mart and screen | M | 2, VZI | Last demo screen becomes real |
| **7** | FR-3 corrections (previous plan §5) on top of the marts | M | 2, 5 | Quantity suggestion and the assistant assessment are instant and correct |
| **8** | Parity tests (mart vs `?live=true`), staleness tests, the performance table from §5 measured and recorded | M | all | Proof |

Steps 0–2 make the screens usable. Steps 3–4 make all of them instant.

---

## 7. Decisions needed

| ID | Decision | Recommendation |
|---|---|---|
| D1 | Measure metrics as of the snapshot date (20-Aug-2026) or today | **Snapshot date.** Without it, precomputed results go stale every day (previous plan, D1) |
| D6 | Serve stale marts or refuse | **Serve, marked stale.** Never recompute inside a request |
| D7 | Total count: header or a changed body envelope | **Header** (`X-Total-Count`), so no existing response shape breaks |
| D8 | Redeployment candidate rule | Needs VZI. The draft is in §3.5 |
| D9 | Frontend cache lifetime | **Until the snapshot changes**, invalidated by tag, not by a timer |

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| Mart and live results differ (for example the summary's no-activity NON_MOVING positions, which have no WATCH row) | Parity tests against `?live=true`, kept permanently |
| A refresh fails halfway | One transaction, or staging tables swapped in at the end; the previous snapshot keeps serving |
| The cache shows old data after a reseed | Snapshot-id check + `revalidateTag`; the stale banner is driven by the backend, not the browser |
| Paging changes what the client-side filters see | Move filters to the server with the paging (URL params already exist) |
| The Redeployment rule is contested | It stays demo-bannered until signed off |
| Docker or Postgres going down again looks like an app bug | The "Database unavailable" error state from §4 |

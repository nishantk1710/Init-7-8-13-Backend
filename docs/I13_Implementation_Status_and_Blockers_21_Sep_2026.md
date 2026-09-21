# Initiative 13 — Implementation Status, Gaps and Blockers

**Work packages covered:** W3.5, W6.1, W6.2, W6.3, W6.4, W6.5, W6.6
**Date:** 21 September 2026
**Source documents:** `VZI_AI_Dev_Plan_v2.4.xlsx` (WS6 + W3.5), `Initiative_13_FRS_v1.1` (FR-1 … FR-11, Sections 5–10)
**Branch assessed:** `feat/sj/init13` @ `a1ef7c1`

---

## 1. How this assessment was produced

This is not a document review. Every claim below was checked against running code and the real seeded SAP extract.

| Check | What was done |
| --- | --- |
| Test suite | `pytest tests/i13` against live Postgres — **249 passed, 0 failed** (16m 49s) |
| Live API | Every I13 endpoint called against the real dataset and the responses inspected |
| Database | Row counts, field fill-rates and join coverage queried directly on the seeded extract |
| Code | Config, business rules and stitching keys read against the FRS requirement text |

**Data baseline:** 45,352 materials · 44,394 OAR material-plant positions · 105,848 reservations · 234,310 PR items · 82,718 PO items · 233,145 goods movements · 157,266 PO history rows.

Two words are used deliberately throughout:

- **Built** — the code exists, is unit- and integration-tested, and behaves correctly.
- **Demonstrable** — it produces a correct, meaningful result *on the real VZI data*.

Several packages are fully **built** but not yet **demonstrable**, and the reason is almost always a data or wiring gap rather than missing code. That distinction is the main message of this document.

---

## 2. Status at a glance

| WP | Scope | Built | Demonstrable on real data | Headline gap |
| --- | --- | --- | --- | --- |
| **W3.5** | Statistics-independent aging | ✅ Complete | ✅ Yes | Unscoped queries take ~4 min; no SQL-side aggregation |
| **W6.1** | PR → PO → GR → GI ledger | ✅ Complete | ⚠️ Partial | Only 37.6% of PO items carry a PR reference **in SAP** |
| **W6.2** | + Reservation leg, OAR scope | ✅ Complete | ❌ Largely not | Only **1.2%** of reservations carry a PR reference; AUFNR leg not built |
| **W6.3** | WATCH metrics + mart | ✅ Complete | ⚠️ Partial | Mart covers 7,184 of 44,394 OAR positions (16%); no daily refresh job |
| **W6.4** | Consumption attribution | ✅ Complete | ✅ Yes (80.6%) | Cost-centre path off — no EKKN/AUFK source |
| **W6.5** | Reclassification evidence | ✅ Complete | ⚠️ Partial | 2 of 3 SOP indicators unavailable (Critical / HOD-justified) |
| **W6.6** | ACT queue + escalation | ✅ Complete | ❌ No | **42,649 exceptions, 0 routed** — owner sourced only from ConsumptionPlan |

**The single highest-value fix in this list is W6.6** (Section 4.7). It is a one-point wiring change, not an external dependency, and it would make ~80% of the exception queue demonstrable immediately.

---

## 3. Five documented blockers that reality has already closed

The Dev Plan and FRS were written 6–11 Sep. The extract now loaded is materially better than either document assumes. **These five items should be closed out rather than carried.**

| # | Document says | Actual state (verified 21-Sep) | Action |
| --- | --- | --- | --- |
| 1 | **FRS §8, §10: "BLOCKING — MARA.EXTWG not exposed. The entire I13 material scope depends on it."** | Superseded by the **08-Sep VZI ruling** (Dev Plan W2.4): OAR = `MARC.DISMM in (ND, PD)`. Implemented and working: PD 36,914 + ND 7,480 = **44,394 OAR positions**. EXTWG is read nowhere in the code. | **Close.** FRS §3.1/§8/§10 need a v1.2 correction — EXTWG is no longer the scope key. |
| 2 | **Dev Plan W6.2: "RESB is empty in this client"** | RESB holds **105,848 rows**. | **Close.** Reservation leg is testable on real data. |
| 3 | **Dev Plan W3.5 / W2.6: "S031 is empty; S032 has 1,484 rows"** | S031 = **54,406** rows, S032 = **20,512** rows. | **Close** — and consider using S031/S032 as an independent cross-check of W3.5 aging, which the FRS originally wanted as the primary source. |
| 4 | **Dev Plan W6.8: "48% of material-plant rows have no MRP type"** | Only **49 of 45,409** MARC rows (0.1%) have a blank MRP type. | **Close.** OAR scope selection is now near-complete. |
| 5 | **FRS §8 / Dev Plan W8.2–W8.3: "dev client holds ~2,034 materials vs 45,000+ in production — supports mechanics validation only"** | Extract holds **45,352 distinct materials**. | **Close / re-open as a question.** This looks like the full population, so the calibration deferral may no longer be justified. Confirm with VZI that this extract is production-representative before recalibrating bands (W8.3). |

---

## 4. Work package detail

### 4.1 W3.5 — Statistics-independent aging

> **Dev Plan:** "Derive aging bands, days since last movement, inventory turns and consumption counts from goods movements directly rather than from the LIS statistics sets."
> **FRS:** FR-1, FR-6; aging bands locked 06-Sep (fast 0–365, slow 366–730, non-moving >730 **days since last issue**, SOP 3.15).

**Implemented**
- All five metrics: `last_movement_date`, `days_since_last_movement`, `aging_band`, `consumption_count_12m`, `inventory_turns`, plus `last_issue_date` / `days_since_last_issue`.
- Bands classify on **days since last issue**, correctly matching SOP 3.15 and FR-6 (not "any movement"). Both dates are exposed separately.
- Thresholds are configuration (`I13_AGING_FAST_MAX_DAYS`, `I13_AGING_SLOW_MAX_DAYS`), validated at startup (fast must be < slow).
- Zero runtime dependency on S031/S032.
- Boundary tests at 365/366/730/731 days and the 12-month window edges all pass.

**Verified live:** material `2000000002` / plant `1300` → FAST, 34 days since last issue, 223 consumption events in 12M. Correct.

**Not implemented / problems**
1. **Performance — the main issue.** `GET /api/i13/summary`, which runs W3.5 across the whole population, took **~4 minutes** for a single request. All 233,145 movement rows are pulled into Python and aggregated there instead of in SQL. Any dashboard calling this will time out. Same pattern makes the Postgres integration tests take ~17 minutes.
2. **Population skew to sanity-check with the business:** of 44,394 OAR positions, **40,868 (92%) classify NON_MOVING**, 3,148 FAST, 378 SLOW. Plausible for slow-moving spares, but it should be confirmed as expected before it reaches a dashboard.
3. **Inventory turns formula is hardcoded** — `consumption_qty_12m ÷ current_stock`, no averaging period, not configurable. FRS §6 lists turns as a required metric but never defines the convention.

**Questions**
- **VZI finance/inventory:** confirm the inventory-turns convention — numerator, denominator (current stock vs. average stock) and averaging period.
- **VZI functional:** is 92% NON_MOVING consistent with expectation for the OAR population?

---

### 4.2 W6.1 — Utilisation ledger, partial (PR → PO → GR → GI)

> **Dev Plan:** "Deterministic stitching of PR to PO to goods receipt to issue on the live sets, without the reservation leg."
> **FRS:** FR-5 — deterministic via RSNUM, AUFNR, BANFN, EBELN. No probabilistic matching (D9).

**Implemented**
- Full PR → PO → GR → GI chain on `EBAN → EKPO → EKBE → MSEG`, PO-item-anchored.
- Correct key mapping: `EKPO.purchase_requisition` / `item_of_requisition` → BANFN/BNFPO.
- **Unmatched legs are reported, never guessed** — exactly as D9 requires. Every broken link carries a status and a human-readable reason (`NO_PR_REFERENCE`, `NO_RECEIPTS`, `UNRESOLVED_PENDING_RESERVATION`).
- Quantity reconciliation with over-receipt clamping.
- A dedicated diagnostics endpoint quantifies match rates.
- Pagination present (`limit`/`offset`, default 100, max 1000).

**Verified live:** `/utilisation-ledger/partial/diagnostics` returns:

| Metric | Value |
| --- | --- |
| PR items total | 197,480 |
| PR items with **no** PO | 178,423 (90.3%) |
| PR items with a single PO | 18,186 |
| PO items total | 77,493 |
| PO items with **no** PR reference | 48,639 (62.8%) |
| PO items with unresolved PR reference | 8,714 |
| Duplicate PR / PO keys | none |

**Problems — this is a data characteristic, not a defect**

Only **31,101 of 82,718 EKPO rows (37.6%)** carry a `purchase_requisition` value *in the SAP source itself*. The stitching code is correct; the source data simply does not support completing most chains through the PR route (consistent with direct or framework orders raised without a formal PR).

**Consequence:** the FRS acceptance criterion *"the ledger stitches reservation, PR, PO, GR and issue deterministically for every OAR acquisition in the twelve-month window"* cannot be met via BANFN for the majority of acquisitions, regardless of build effort.

**Questions**
- **VZI / SAP functional:** is a 62.8% no-PR-reference rate on PO items expected for this population? Are these predominantly framework/direct orders?
- **VZI:** for POs with no PR, is the chain expected to start at the PO — i.e. should "PO without PR" be a valid complete chain rather than an unmatched one?

---

### 4.3 W6.2 — Reservation leg + OAR scope

> **Dev Plan:** "Add the reservation leg to the ledger and apply the OAR material scope through the W2.4 config filter. Completes CAPTURE and STITCH."
> **FRS:** FR-5, D9, D8.

**Implemented**
- Reservation → PR → PO → GR → GI, reservation-anchored (`RESCHAIN-{RSNUM}-{RSPOS}`).
- OAR scope applied at the API boundary from W2.4 config (`I13_OAR_MRP_TYPES=ND,PD`), with `include_out_of_scope` as an explicit override.
- Goods issues attributed by RSNUM/RSPOS, split into `procurement_issued_quantity` vs `direct_store_issued_quantity`.
- Reservation source is switchable (mock ↔ Postgres) without touching ledger logic — the W6.2 design requirement.
- Pagination present.

**Verified live:** ledger entries build correctly, OAR scope is applied, unmatched legs carry explicit reasons.

**Not implemented**
- **The AUFNR (order) stitching leg.** FR-5 and D9 name **RSNUM, AUFNR, BANFN, EBELN** as the four deterministic keys. Only three are used. `RESB.order` (AUFNR) is read and carried into W6.4 attribution, but is **not used as a stitching key**.

**Problems — the most significant data blocker in the initiative**

Only **1,274 of 105,848 reservations (1.2%)** carry a `purchase_requisition` value. The Reservation → PR hop — the join that completes CAPTURE→STITCH — resolves for barely one reservation in a hundred.

This is the risk the 11-Sep Implementation Plan flagged ("validate with mock; re-run against a fuller client when data becomes available"). Real data is now available, and the linkage is still absent. In standard SAP, RESB does not normally carry a PR reference; reservation-to-procurement linkage usually runs through the **order (AUFNR)** or the account assignment, not through RESB-BANFN.

**Field availability on RESB (105,848 rows):**

| Field | Populated | % | Relevance |
| --- | --- | --- | --- |
| `goods_recipient` (WEMPF) | 77,457 | **73.2%** | Requester proxy — already used by W6.4 |
| `order` (AUFNR) | 41,538 | **39.2%** | **FR-5 stitching key — not yet used as one** |
| `g_l_account` | 40,642 | 38.4% | Cost-object context |
| `text` | 25,806 | 24.4% | Candidate session-ID field — partly occupied |
| `item_text_line_1` | **0** | 0% | **Empty — clean candidate for the session-ID field** |
| `purchase_requisition` | 1,274 | **1.2%** | Current stitch key — near-unusable |

**Recommendation:** implement the **AUFNR leg** (39.2% coverage, already specified in FR-5, data already extracted). It is a ~30× improvement over the current BANFN path and requires no SAP change.

**Questions**
- **VZI / SAP functional — highest priority:** how is an OAR reservation actually linked to its downstream PR/PO in VZI's process? Via AUFNR? Via account assignment? Or is the link genuinely only established by the session identifier once the BAdI is live?
- **SAP team:** `RESB.item_text_line_1` is empty across all 105,848 rows — is it available for the session identifier (FRS §4.1 asks for exactly this designation), or is a Z-append preferred?

---

### 4.4 W6.3 — WATCH metrics

> **Dev Plan:** "Months of cover, acquired versus plan, 30-day goods-received-not-issued, aging bands as configurable SOP defaults. Utilisation marts on live data."
> **FRS:** FR-6 — "Refresh daily; reconcile monthly against the ZMM065 report and the 30-Day GR Report."

**Implemented**
- All four metrics: months of cover (+ projected, net of open POs), acquired-vs-plan, 30-day GRNI, aging bands.
- Persisted mart (`i13_watch_metric_mart`) with a fast read path — `/api/i13/act/utilisation` returns in **0.26s** with correct filtering.
- Zero-consumption handled correctly: cover is `null` with reason `INSUFFICIENT_HISTORY`, never zero or infinity.
- GRNI computed at **remaining-quantity level** (`received − issued > 0`), aged from the oldest outstanding GR — a defensible convention, already consistent.
- Reconciliation endpoint exists with a configurable tolerance (`I13_RECONCILIATION_TOLERANCE_PCT=5.0`).

**Not implemented**
1. **No daily refresh.** FR-6 requires a daily refresh; there is no scheduler anywhere in the codebase. The mart is only populated when a refresh is invoked manually.
2. **No reference data for reconciliation.** `/api/i13/validation` returns `REFERENCE_UNAVAILABLE` for both ZMM065 and the 30-Day GR Report — the counts must currently be passed in by hand as query parameters. Computed side works (255,916 procurement entries; 179 GRNI).
3. **Acquired-vs-plan is untestable** — see the plan-data blocker in Section 5.

**Problems**
1. **Mart coverage is 16%.** `i13_watch_metric_mart` holds **7,184 rows against 44,394 OAR positions**, covering only plants 1300 and 1200. Any dashboard reading the mart today shows a partial picture.
2. **`GET /api/i13/watch` (the live-compute path) silently ignores unknown query parameters.** `?grni=true&limit=3` returned the **entire unfiltered dataset — 13.3 MB**, because neither parameter is declared on that route. The mart-backed `/act/utilisation` supports `grni` but has no `limit`. Two near-identical endpoints with different contracts and no validation.
3. **No tolerance band on acquired-vs-plan** — exact comparison by deliberate design (the code says so explicitly). FRS does not define whether small variances count as a breach.

**Questions**
- **VZI:** reconciliation tolerance against ZMM065 and the 30-Day GR Report (FRS §10 open item — default 5% currently).
- **VZI:** does acquired-vs-plan need a tolerance band, or is exact comparison correct?
- **Architecture:** where does the daily refresh run — Azure Function, App Service WebJob, or Data Factory trigger?

---

### 4.5 W6.4 — Consumption attribution

> **Dev Plan:** "Consumption attribution via reservation and order data; cost-centre path behind a config flag."
> **FRS:** FR-4 (requester/material/plant/date against session), §7 fallback: "until EKKN/AUFK are confirmed, cost-centre attribution uses RESB and order data."

**Implemented — the strongest package in the set**
- Attribution from the reservation line: requester (`WEMPF`), order (`AUFNR`), cost centre (gated).
- Conflict detection: disagreeing requester/order values across a business key are reported as unresolved rather than silently picking one.
- Cost-centre path correctly behind `I13_COST_CENTRE_ATTRIBUTION_ENABLED` (off — no EKKN/AUFK source loaded), exactly as the FRS fallback specifies.
- Persisted mart + pagination.

**Verified live — attribution resolution across 92,684 ledger entries:**

| Status | Rows | With requester | With order |
| --- | --- | --- | --- |
| ATTRIBUTED | 17,148 | 17,148 | 17,148 |
| PARTIALLY_ATTRIBUTED | 75,536 | 57,539 | 15,191 |
| **Total with a requester** | — | **74,687 (80.6%)** | 32,339 (34.9%) |

**This is the key fact for W6.6:** a requester identity is already resolved for **80.6%** of ledger entries.

**Not implemented**
- Cost centre — no deterministic source. `EKKN` and `AUFK` remain "proposed additions carried open" (FRS §7). The seam exists; only the adapter is missing.

**Questions**
- **VZI / SAP team:** confirm EKKN and AUFK for exposure, or confirm that `RESB.g_l_account` (38.4% populated) is an acceptable cost-object substitute.
- **VZI:** is `WEMPF` (goods recipient) an acceptable proxy for "requester" for accountability and routing purposes? It is the only person-like field on RESB.

---

### 4.6 W6.5 — Reclassification evidence

> **Dev Plan:** "Reclassification evidence per SOP 3.1.1: twelve-month consumption count, Critical flag and HOD-justified flag per OAR material. Advisory only."
> **FRS:** FR-8.

**Implemented**
- All three SOP 3.1.1 indicators modelled and OR'd, with per-candidate supporting counts and reasons.
- Consumption threshold configurable (`I13_RECLASS_MIN_CONSUMPTION_COUNT=4`), compared with strict `>` — correctly "more than four", i.e. effectively ≥5.
- Criticality read through the shared W3.4 port (not re-implemented locally).
- Persisted mart — 44,394 rows, matching the OAR population exactly.
- **No fabrication:** unavailable indicators return `null` with `data_available: false` rather than defaulting to false.

**Verified live:** material `2000000002`/`1300` → `candidate_flag: true`, reason `FREQUENT_CONSUMPTION`, 223 consumptions vs threshold 4. Correct.

**Problems**
- **Only 1 of 3 SOP indicators can actually fire.** `critical_impact_indicator` and `hod_justified_request_indicator` both return `null`, so `data_available` is `false` on tested candidates. Consequently **483 candidates** are currently flagged on consumption frequency alone.
  - *Critical flag* depends on the W3.4 criticality source resolving for these materials.
  - *HOD-justified flag* depends on W6.6's confirmation workflow producing records — which it cannot yet (Section 4.7).
- FRS caveat carried correctly: advisory only, decision stays with VZI.

**Questions**
- **VZI:** confirm the criticality tiers that count as "Critical" for SOP 3.1.1. The code defaults to `CRITICAL` only and deliberately excludes `IMPACT`, since the FRS wording ("Critical **or significant production impact**") is broader than the Dev Plan's ("Critical flag"). This is a live discrepancy between the two documents.

---

### 4.7 W6.6 — ACT: exception queue, confirmation and escalation

> **Dev Plan:** "Utilisation and aging APIs, plan-breach and no-plan detection, quantity-override exceptions, requester confirmation and HOD escalation after a configurable period."
> **FRS:** FR-7, FR-9, FR-11; acceptance: "Every exception routes to the requester and escalates to the HOD after the configured period."

**Implemented — the machinery is complete and correct**
- Detection for PLAN_BREACH, NO_PLAN and the 30-day GRNI fallback; NO_PLAN sub-reasons (`MISSING_SESSION`, `INVALID_SESSION`).
- A validated state machine — `OPEN → AWAITING_REQUESTER → CONFIRMED/ESCALATED → RESOLVED`, with no code path that assigns a status without validating the transition. **Verified live:** a confirmation POST on an `OPEN` exception is correctly rejected with `409`.
- Append-only audit trail of every state change with actor and timestamp (FR-9 requirement met).
- Structured requester confirmation (reason category + free text).
- Idempotent detection with business-key dedup; resolved exceptions are not reopened.
- Cross-plant stock visibility on each exception (D10).
- Configurable escalation period (`I13_REQUESTER_RESPONSE_DAYS=5`).

**The blocker — one wiring gap, not an external dependency**

**42,649 exceptions exist. All are `OPEN`. Not one has an owner. `POST /run/escalate` returns `escalated: 0, routing_pending: 0`.**

Root cause, traced in `app/initiatives/i13/act/service.py`:

```
line 350:  owner_requester_id = plan.requester              # PLAN_BREACH
line 391:  owner_requester_id = plan.requester if plan else None   # NO_PLAN
line 421:  owner_requester_id = plan.requester if plan else None
line 456:  owner_requester_id = record.requester_id         # quantity override (W7.4 — not built)
```

Every exception owner comes from `ConsumptionPlan.requester`. For a **NO_PLAN** exception there is, by definition, no plan — so the owner is always `None`, the exception can never transition to `AWAITING_REQUESTER`, and escalation has nothing to act on. All 42,649 current exceptions are NO_PLAN.

Meanwhile **W6.4 already resolves a requester for 80.6% of ledger entries** from `RESB.WEMPF`, and ACT detection never consults it.

**Fix:** have ACT detection fall back to the W6.4 attribution requester when no plan requester exists. This is a single integration point and would make roughly 80% of the exception queue routable and the whole confirm/escalate loop demonstrable — without any SAP change, BAdI, or VZI decision.

*Caveat to state plainly:* `WEMPF` is a goods-recipient name field, not a verified identity. It unblocks **demonstrability**; delivering an actual email still needs the identity mapping in Section 6.

**Also not implemented**
- **Quantity-override exceptions** — depend on W7.4 (quantity-suggestion engine), which is not built.
- **Real notifications** — `LoggingNotificationAdapter` only; nothing is sent. Channels are a hardcoded enum (`PLATFORM_QUEUE`, `EMAIL`), correctly matching D11, but no adapter exists for either.
- **HOD routing** — `I13_HOD_RECIPIENTS` is an empty `"PLANT:identity,…"` string. Empty by design, so routing reports `PENDING` rather than inventing a recipient.
- **No authentication.** `get_current_actor` reads an `X-Actor-Id` HTTP header and trusts it. Entra SSO (W1.6) is not implemented, so the FR-9 audit trail currently records a client-supplied string as the actor.

**Questions**
- **VZI / HOD process owner:** confirm the requester response period before escalation (currently defaulted to 5 days).
- **VZI:** confirm the HOD-per-plant mapping (DOA), and whether `WEMPF` is an acceptable routing target in the interim.

---

## 5. Data-side blockers

Ordered by impact on delivery.

### B1 — Reservation → PR linkage is 1.2% *(blocks W6.2, cascades to W6.3–W6.6)*
Only 1,274 of 105,848 reservations carry a PR reference. The CAPTURE→STITCH chain breaks at the first hop for 98.8% of reservations.
**Needs:** a VZI/SAP functional answer on how reservations actually link to procurement (Section 4.3). **Interim mitigation available in code:** build the AUFNR leg (39.2% coverage).

### B2 — Consumption plans are orphaned synthetic data *(blocks acquired-vs-plan and PLAN_BREACH validation)*
`data-generator/generated/platform/consumption_plans.csv` holds **742 rows left over from the retired synthetic generator**. They cannot join to real data:

| Field | Plan CSV | Real extract |
| --- | --- | --- |
| Plant | `4000` | `1300`, `1500`, `1200`, … — **plant 4000 does not exist (0 rows)** |
| Reservation | `1000000000`, `1000000001` | `219937`, `829147` — **0 matches** |
| Material | `000000000030000000` (18-char) | `2000000002` (10-digit) |

**Consequences:**
- `acquired_vs_plan_status` is `NO_PLAN` for 100% of real material-plants.
- **`plan_breach_count: 313` on `/api/i13/summary` is a false KPI** derived entirely from these orphaned rows. It should not be shown to anyone.
- PLAN_BREACH detection has never executed against a valid plan→reservation join.

**Needs:** the file removed or replaced. Real plans can only come from W7.3/W7.5 (assistant + session issuance), which are not built. **Recommendation:** delete the synthetic file now so the false KPI disappears, and seed a small hand-built plan set that joins to real reservations purely to validate the PLAN_BREACH path before UAT.

### B3 — Material scope covers 2 plants; transactions span 13 *(blocks population completeness)*
`MARC` — the sole source of MRP type and therefore of OAR scope — covers only:

| MARC plant | Rows |
| --- | --- |
| 1300 | 45,352 |
| 1200 | 57 |

But transactions exist far beyond that:

| Plant | MSEG | RESB | EKPO | In MARC? |
| --- | --- | --- | --- | --- |
| 1500 | **122,868** | 10,333 | 35,083 | ❌ **No** |
| 1300 | 99,394 | 92,673 | 45,797 | ✅ |
| 1600 | 4,645 | 339 | 772 | ❌ |
| 1200 | 2,738 | 590 | 31 | ✅ |
| 3000 | 1,565 | 1,526 | 498 | ❌ |
| 1800 | 1,464 | — | 154 | ❌ |
| 2000 | 408 | 385 | 331 | ❌ |
| Others (1700, 1320, 1820, 1100, 1900, 3400, GERG) | small | small | small | ❌ |

**Plant 1500 has more goods movements than 1300** yet has zero MARC rows. Every 1500 material therefore classifies `EXCLUDED` and is invisible to OAR scope, WATCH, exceptions and reclassification. The FRS states four in-scope plants (Gamsberg, BMM + two unnamed); the extract supports scope selection for at most two.

**Needs:** (a) the four in-scope plant codes from VZI; (b) the MARC extract extended to cover them — 1500 at minimum.

### B4 — WATCH mart covers 16% of the OAR population *(blocks dashboard completeness)*
7,184 mart rows vs 44,394 OAR positions, plants 1300/1200 only. Partly a consequence of B3, partly because refresh has only ever been run scoped. **Needs:** a full-population refresh, then the daily job (Section 4.4).

### B5 — Two of three reclassification indicators cannot fire *(degrades W6.5)*
Critical flag and HOD-justified flag both resolve to `null`. The HOD flag is circularly blocked on W6.6's confirmation workflow. See Section 4.6.

### B6 — Reconciliation reference data absent *(blocks FR-6 acceptance)*
No ZMM065 export or 30-Day GR Report is loaded, so `/api/i13/validation` cannot self-reconcile. Counts must be typed in by hand. **Needs:** both reports as files, plus the agreed tolerance.

### B7 — `data-sources` under-reports MSEG by 56% *(cosmetic but misleading)*
`raw_mseg` was loaded in two batches (102,631 + 130,514 = 233,145) with identical timestamps. `GET /api/i13/data-sources` picks one arbitrarily via a non-deterministic `ORDER BY finished_at DESC LIMIT 1` tie-break and reports **102,631** against an actual **233,145**. Also affects `raw_cdhdr`, `raw_s031`, `raw_s032`. **Fix:** sum all successful runs per table. Small code change.

---

## 6. Connections, credentials and IDs required

### 6.1 Azure platform — W1.1/W1.2, currently the deployment gap

| Item | Env var | Status | Blocks |
| --- | --- | --- | --- |
| Azure SQL — `sqldb-aicom` on `sql-vzi-aicom-nonprod-san` | `DATABASE_URL` | Unreachable outside the VNet (private endpoint, public access disabled) | All deployed I13 endpoints. *Local Postgres is the only working data source today.* |
| ADLS Gen2 — `stvziaicomnonprod` via `pe-stvziaicomnonprod-dfs` | `STORAGE_URL` | **Not working.** Must be `abfss://…`; a local path is refused. `/api/ready` reports `storage: unavailable`. | `python -m app.seed` — the entire seeding path on Azure |
| Managed identity + **Storage Blob Data Reader** on the storage account | — | Required; without it every read is a 403 that looks like a missing file | Seeding |
| ODBC Driver 18 for SQL Server | — | System-level, not a pip package | Azure SQL connectivity |
| Key Vault | `KEY_VAULT_URL` | Used only by `python -m app.checkup` | Diagnostics only |
| **`psycopg`** | — | **Missing from `requirements.txt`** although `app/core/db.py` supports Postgres. Had to be installed by hand. | Local development |

> **Verification command:** `python -m app.checkup` from inside the App Service reports database, storage, CPI and model separately and prints no secrets.

### 6.2 SAP / CPI

| Item | Env var | Status | Blocks |
| --- | --- | --- | --- |
| CPI OAuth client credentials | `CPI_BASE_URL`, `CPI_TOKEN_URL`, `CPI_CLIENT_ID`, `CPI_CLIENT_SECRET` | Not set locally; proven from a workstation per W2.1 | Live delta ingestion. **Not** blocking today — the seeded extract is the data source. |
| iFlow path | `CPI_PATH` | Defaulted | — |
| Corporate CA bundle | `CPI_CA_BUNDLE` | Only needed on a TLS-intercepting network | — |

### 6.3 Identity — the gap that blocks W6.6 end-to-end

| Item | Env var | Status | Blocks |
| --- | --- | --- | --- |
| Entra tenant / app registration | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` | Declared, **read by no code**. W1.6 SSO not implemented. | Real actor identity on the FR-9 audit trail |
| Authenticated user | — | `X-Actor-Id` header, trusted as-is | Audit integrity |
| **Requester identity mapping** | — | `WEMPF` → 80.6% coverage, but no mapping to an Entra identity or mailbox | **Email notification** (FR-11). *Not* routing itself — see Section 4.7. |
| **HOD / DOA mapping per plant** | `I13_HOD_RECIPIENTS` | Empty; routing reports `PENDING` | HOD escalation (FR-9) |
| Email / platform-queue adapter | — | `LoggingNotificationAdapter` only — nothing is sent | FR-11 |

### 6.4 SAP-side changes — still outstanding (FRS §4.1, §10)

| Item | Owner | Blocks | Note |
| --- | --- | --- | --- |
| **Session-ID field designation on the reservation** | SAP team | W7.6 traceability, and genuine NO_PLAN detection | `RESB.item_text_line_1` is **empty across all 105,848 rows** — a clean candidate |
| **Reservation-entry BAdI** (shared with I08) | SAP team + NTT | BAdI-triggered capture | Not in any ABAP stream; assistant can run on demand without it |
| `EBAN.BNFPO` added to the entity key | SAP team | Delta ingestion via OData | Not blocking today — the seeded extract carries both columns |
| `EKBE`, `EKKN`, `AUFK`, `MARDH`, `MBEWH` exposure | SAP team | Cost-centre attribution; 12-month baseline | **`EKBE` is already in the extract (157,266 rows)** and in use — it can be closed |
| LIS technical field names for S031/S032 | SAP team | Only if S031/S032 are reinstated as a cross-check | W3.5 removed the hard dependency |
| `$count` / generic `$filter` / default `ORDER BY` | SAP team | Delta ingestion | Full ordered pulls work today |

### 6.5 Business data required from VZI

| Item | Needed for | Priority |
| --- | --- | --- |
| The four in-scope plant codes | B3 — scope correctness | **High** |
| ZMM065 monthly export + 30-Day GR Report | B6 — FR-6 acceptance | **High** |
| Reconciliation tolerance | FRS §10 open item | Medium |
| HOD-per-plant mapping | FR-9 escalation | **High** |
| Criticality tier definition for SOP 3.1.1 | W6.5 | Medium |
| Confirmation that this extract is production-representative | W8.3 recalibration | Medium |

---

## 7. Consolidated open questions by owner

**VZI / SAP functional — blocking**
1. How does an OAR reservation actually link to its PR/PO? (`RESB.BANFN` = 1.2%; `AUFNR` = 39.2%) — **B1**
2. Which four plant codes are in scope, and can MARC be extended to cover them (1500 especially)? — **B3**
3. Is `WEMPF` (goods recipient) an acceptable requester proxy for accountability and routing?

**SAP team**
4. Designate the session-ID field — is `RESB.item_text_line_1` (empty) available, or is a Z-append preferred?
5. Reservation-entry BAdI: confirm the ABAP stream and transport timing.
6. Confirm EKKN/AUFK exposure, or confirm `RESB.g_l_account` as the cost-object substitute. *(EKBE can be closed — already delivered.)*

**VZI business / SOP owner**
7. Requester response period before HOD escalation (default 5 days).
8. HOD-per-plant (DOA) mapping.
9. Inventory-turns convention — numerator, denominator, averaging period.
10. Reconciliation tolerance against ZMM065 / 30-Day GR Report (default 5%).
11. Acquired-vs-plan: exact comparison, or is a tolerance band allowed?
12. Criticality tiers counting as "Critical" — `CRITICAL` only, or `CRITICAL` + `IMPACT`? *(FRS and Dev Plan differ.)*
13. Is 92% NON_MOVING expected for the OAR population?
14. Cover ceiling, minimum history and look-back for the quantity suggestion (W7.4, not yet built).
15. Session validity window.

**Documentation**
16. FRS v1.2 should record that EXTWG is superseded by the DISMM ruling, and that S031/S032 are no longer the aging source.

---

## 8. Suggested order of work

**Immediate — no external dependency, high payoff**
1. **Wire ACT's exception owner to W6.4 attribution** (Section 4.7). Unblocks ~80% of the exception queue and makes confirm/escalate demonstrable. *Single highest-value change available.*
2. **Delete the orphaned synthetic `consumption_plans.csv`** — removes the false `plan_breach_count: 313` KPI (B2).
3. **Build the AUFNR stitching leg** (FR-5, 39.2% coverage) — ~30× the reservation linkage of the current path.
4. **Fix `data-sources` row counts** — sum all successful ingestion runs (B7).
5. **Fix `/api/i13/watch`** — declare `grni`/`limit`/`offset` or reject unknown parameters; add pagination to `/act/utilisation`, `/act/exceptions`, `/reclassification` and the legacy `/exceptions`.

**Short term — engineering**
6. Move W3.5 and summary aggregation into SQL (removes the ~4-minute `/summary`).
7. Full-population WATCH mart refresh, then schedule the daily job (FR-6).
8. Implement the email / platform-queue notification adapters behind the existing port.

**Dependent on VZI / SAP**
9. Resolve the reservation→procurement linkage question (B1) — determines whether W6.2 can ever meet its acceptance criterion.
10. Extend MARC to the in-scope plants (B3).
11. Load ZMM065 and the 30-Day GR Report (B6) and close FR-6 acceptance.
12. Entra SSO (W1.6), then replace the `X-Actor-Id` header with real claims.

**Downstream / not started**
13. W7.3 assistant + W7.5 session issuance — the only real source of ConsumptionPlan records, and therefore the prerequisite for genuine NO_PLAN and PLAN_BREACH semantics.
14. W7.4 quantity-suggestion engine — prerequisite for quantity-override exceptions in W6.6.

---

## Appendix — evidence summary

| Measure | Value |
| --- | --- |
| I13 test suite | 249 passed, 0 failed (16m 49s, live Postgres) |
| OAR positions (ND + PD) | 44,394 (PD 36,914 · ND 7,480) |
| Distinct materials / MARC plants | 45,352 / 2 (1300, 1200) |
| Aging split | FAST 3,148 · SLOW 378 · NON_MOVING 40,868 |
| EKPO with PR reference | 31,101 / 82,718 (37.6%) |
| RESB with PR reference | 1,274 / 105,848 (**1.2%**) |
| RESB with AUFNR | 41,538 / 105,848 (39.2%) |
| RESB with WEMPF | 77,457 / 105,848 (73.2%) |
| Attribution with requester | 74,687 / 92,684 (**80.6%**) |
| ACT exceptions / routed | 42,649 / **0** |
| WATCH mart coverage | 7,184 / 44,394 OAR (16.2%) |
| Consumption plans joining real data | **0 / 742** |
| `/api/i13/summary` response time | **~4 minutes** |
| `/api/i13/act/utilisation` response time | 0.26s |

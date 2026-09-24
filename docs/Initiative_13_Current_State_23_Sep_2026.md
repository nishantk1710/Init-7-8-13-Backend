# Initiative 13 — Current State of Implementation

**End-to-End Spares Utilization Tracking Optimization**

| | |
|---|---|
| **Date** | 23 September 2026 |
| **Reference FRS** | `Initiative_13_FRS_v1.1 (1).docx` (06-Sep-2026), with `Initiative_13_FRS_v1.2_final.docx` corrections noted inline |
| **Backend** | `Init-7-8-13-Backend`, branch `feat/vp/ws7-assistant` @ `13cd652` |
| **Frontend** | `Init-7-8-13-Frontend`, branch `feat/vp/ws7-assistant-frontend` @ `65ea053` |
| **Method** | Read directly from the checked-out source. Measured figures are quoted from `I13_Implementation_Status_and_Blockers_21_Sep_2026.md`, `WS7_Assistant_Implementation_Notes.md` and FRS v1.2 §7, which were produced against the live seeded database. |

---

## 0. The one-paragraph version

The CAPTURE → STITCH → WATCH → ACT loop is **built end to end**, in Python, with tests, against a real 3.4M-row SAP extract — and a working Next.js dashboard suite on top of it. The engines are correct. What limits Initiative 13 today is not missing code; it is **four data facts and one wiring gap**: the reservation→PR join resolves for 1.2% of reservations, MARC covers only one of the two in-scope plants, goods-movement history spans 12.5 months against the 731 days the aging bands need, every consumption plan in the reference data is fabricated, and **42,649 ACT exceptions exist with zero owners** because detection reads the plan's requester and never the attribution requester it already has for 80.6% of ledger entries. Nothing on SAP's side blocks the last of those.

---

## 1. Where the code lives

```
Init-7-8-13-Backend/
  app/
    api/i13/            # HTTP routes — thin, no computation
      routes.py         #   mounts every sub-router under /api/i13
      ledger.py  reservation_ledger.py  procurement_chain.py
      movement_metrics.py  watch.py  exceptions.py
      reclassification.py  validation.py  consumption_attribution.py
      act.py            #   /api/i13/act/* — the FR-9 queue
      assistant.py      #   WS7: consumption plans + FR-3 on its own
      deps.py           #   get_data_dir, get_current_actor (X-Actor-Id)
    api/assistant/      # /api/assistant/* + /api/justifications (shared I08+I13)
    assistant/          # WS7 domain: router, ids, session, script, steps,
                        #   turns, narrative, intents, models
    initiatives/i13/    # ALL business logic
      config.py         #   typed config objects built from Settings
      models.py         #   frozen dataclasses + every enum
      movements.py movement_metrics.py aging.py     # W3.5
      procurement_chain.py                          # W6.1
      reservation_ledger.py                         # W6.2
      watch.py watch_mart.py                        # W6.3
      attribution.py consumption_attribution*.py    # W6.4
      reclassification.py reclassification_mart.py  # W6.5
      act/  (domain, detection, state_machine, service, ports)  # W6.6
      act_exception_store.py act_hod_provider.py
      act_notifications.py act_stock_provider.py act_watch_snapshot.py
      quantity.py                                   # FR-3
      reservation_assistant.py                      # FR-2 assessment
      plans.py exceptions.py summary.py reconciliation.py
    integrations/sap/postgres_*.py   # read-only Postgres adapters
    shared/material_scope/policy.py  # OAR / MIN_MAX / EXCLUDED
    shared/plant_scope.py            # plants 1300 + 1500, enforced in SQL
    schemas/i13.py  schemas/i13_act.py   # Pydantic response models

Init-7-8-13-Frontend/
  src/features/initiative-13/   # manifest, pages, components, data loaders
  src/lib/api/i13.ts            # typed client for /api/i13/*
  src/lib/api/assistant.ts      # typed client for /api/assistant/*
  src/app/oar-utilization/**    # the 11 route entries
  src/app/assistant/**          # the shared assistant screens
```

**Layering rule, enforced throughout:** routes never compute. Every number is produced in `app/initiatives/i13/` and serialised by a route. The frontend never recomputes a business rule either — no aging band is reclassified in the browser, no months-of-cover recalculated — so the dashboard and the exception queue cannot disagree about the same material.

---

## 2. The data foundation

### 2.1 Everything reads Postgres, not OData

The FRS assumes live OData reads through the CPI generic consumption endpoint. **In practice, all I13 reads go to a seeded Postgres database** loaded by `python -m app.seed` from the July 2026 extract. The SAP client (`app/integrations/sap/client.py`) exists and is exercised by drift tests, but no I13 endpoint calls it. `GET /api/i13/data-sources` reports per-table ingestion status from the `ingestion_run` table — it replaced the old LIVE/MOCK gateway diagnostic.

### 2.2 The OAR scope rule — the biggest deviation from FRS v1.1

FRS v1.1 §3.1 identifies OAR from **`MARA.EXTWG`** and records its absence as the single BLOCKING dependency. That is **superseded**.

The VZI ruling of 08-Sep-2026 (Dev Plan v2.4, W2.4) defines OAR as **`MARC.DISMM ∈ (ND, PD)`**. It is implemented in `app/shared/material_scope/policy.py`, driven by config (`I13_OAR_MRP_TYPES=ND,PD`, `I13_MIN_MAX_MRP_TYPES=VB`), and **`EXTWG` is read nowhere in the codebase**. FRS v1.2 §7 records the correction; v1.1 §3.1/§8/§10 are stale on this point.

Consequence, measured on the seeded extract:

| MRP type | Rows | Share |
|---|---:|---:|
| PD | 36,914 | 81.3% |
| ND | 7,480 | 16.5% |
| VB (Min-Max) | 950 | 2.1% |
| blank (→ Excluded) | 49 | 0.1% |
| V1 (→ Excluded) | 16 | 0.0% |

**44,394 OAR material-plant positions.** Scope is per material *and* plant, because DISMM is a MARC (plant-level) field — the same material can be OAR at one site and Min-Max at another. Had EXTWG remained the key, scope would have been ~3,525 materials.

> **Open question worth raising:** ND+PD selects **97.8%** of the catalogue. The assistant will therefore ask for a consumption plan on almost every part, not half of them. This is one config line, but it needs VZI confirmation.

### 2.3 Plant scope — two plants, enforced in SQL

`app/shared/plant_scope.py` (ruling of 21-Sep-2026) restricts the platform to **1300 Black Mountain Mining** and **1500 Gamsberg**. Filtering happens in the WHERE clause of every `postgres_*.py` adapter (`sql_predicate("plant")`), not after aggregation — so counts, aging bands and reconciliation percentages are computed on the scoped population to begin with.

FRS v1.1 still says "four in-scope plants". The codebase says two. The four codes are still an open item with VZI.

### 2.4 Raw tables actually read

| Table | Read for |
|---|---|
| `raw_marc` | DISMM → OAR scope; the material-plant key master |
| `raw_mard` | Unrestricted stock (LABST) — stock on hand, cross-plant stock |
| `raw_mseg` + `raw_mkpf` | Goods movements: consumption, aging, GI linkage. Joined on `material_document` + `material_doc_year`; posting date from MKPF |
| `raw_resb` | Reservations — the demand anchor |
| `raw_eban` | Purchase requisitions |
| `raw_ekpo` | PO items, ordered quantity, BANFN/BNFPO stitch key |
| `raw_ekbe` | PO history filtered to VGABE = E — received quantity net of reversals |
| `raw_zmm065_*` | Criticality tiers, via the shared W3.4 criticality port |

Movement-type vocabulary (`app/initiatives/i13/movements.py`): receipts `101`, issues `201`/`261`, reversals `102→101`, `202→201`, `262→261`, netted before any metric is computed.

Seeded but **not read by any endpoint**: `mara`, `makt`, `mbew`, `mchb`, `ekko`, `eket`, `eina`, `eine`, `lfa1`, `cdhdr`, `cdpos`, `s031`, `s032`. S031/S032 were the FRS's system-derived aging source; W3.5 removed that dependency entirely and computes aging from goods movements directly.

### 2.5 Non-SAP input

`<i13_data_dir>/platform/consumption_plans.csv` — **742 fabricated rows** with invented `SESS-000001` references, plant `4000` (which does not exist), 18-character material numbers and reservation numbers that match nothing. **Zero of 742 join to real data.** They are still the input to acquired-vs-plan and PLAN_BREACH detection, which is why `plan_breach_count` on `/api/i13/summary` is a false KPI. `app/initiatives/i13/plans.py` tags every plan with `PlanSource.CAPTURED` or `REFERENCE_CSV` so the two can be told apart — the endpoint that lists captured plans deliberately does **not** merge them.

---

## 3. The domain engines, in loop order

### 3.1 CAPTURE — the reservation-time assistant (WS7, FR-2/3/4)

Built on branch `feat/vp/ws7-assistant`, shared with Initiative 08 and mounted at `/api/assistant/*` rather than under either initiative — the SAP pop-up knows a material and a plant and cannot know which flow applies, so it cannot pick a prefix.

**Routing** (`app/assistant/router.py`): 80-series → I08 flow; OAR by DISMM → I13 flow; neither → no session minted at all (a 200 with a null session, not a 404). **97.6% of 80-series material-plant rows are also OAR**, so precedence is not a tie-break — it decides which flow essentially every repairable gets. I08 wins; the losing match is recorded on `RoutedFlow.also_matched` so the decision stays reviewable.

**Session identifier** (`app/assistant/ids.py`): `S` + 8 Crockford-base32 characters + 1 check character = **10 characters**, sized to SAP's `Bednr` (`Edm.String(10)`). The alphabet drops `I/L/O/U`; a typed `O` is repaired to `0`. The check character uses odd position weights, catching every single-character substitution and every adjacent transposition except pairs exactly 16 apart (~3% of swaps, documented rather than claimed away). This matters because "no such session" is the exact compliance finding I13 raises for somebody who skipped the assistant — without a checksum a typo becomes a false accusation.

**The I13 conversation** (`app/assistant/script.py`), server-driven and stateless — the next step is derived from the answers so far, never from a stored cursor:

```
assessment (FR-2a cross-check)  →  "go ahead with this reservation?"
   ├─ no  → terminal: nothing captured, advice still recorded
   └─ yes → capture plan (FR-2b/FR-4): purpose, planned qty,
            window start/end, cost centre, work order
              → quantity suggestion (FR-3)
                   ├─ no suggestion possible → terminal, with the reason
                   ├─ not an override        → terminal
                   └─ override → "take the suggested N?"
                        ├─ accept → terminal
                        └─ keep   → justification (category + free text) → terminal
```

**The FR-2(a) assessment** (`app/initiatives/i13/reservation_assistant.py`) composes an existing WATCH row and the cross-plant stock provider and adds **no new computation** — stock here, quantity on order, months of cover, days since last movement + aging band, stock at other plants. Plants holding zero are dropped from the sentence. Absences are stated as absences: "no stock record exists" ≠ zero, and undefined cover is reported as effectively unlimited, never as 0.

**Five append-only tables** (Postgres triggers block UPDATE/DELETE): `assistant_session`, `assistant_turn`, `consumption_plan`, `justification`, `quantity_suggestion`. Session outcome is **derived from turns, never stored** — so an abandoned session ("advice given, not acted on", a number both FRSs want) is visible without anything having written a status to an immutable log.

**The narrative layer** (`ASSISTANT_NARRATIVE_ENABLED=false`) is built and off. Every number the assistant states is a field already held; the arithmetic is never the model's job. This is a visible deviation from the FRS's "responses generated through the provider-agnostic LLM layer" and needs sign-off before anything depends on it. Two safety rules are in code: a provider failure degrades to the deterministic sentence, and the stub provider's placeholder prose is refused by name.

**The free-text box** (`/api/assistant/ask`) answers exactly three questions from deterministic read models — overdue repairs, OAR materials with no plan, what is waiting on a decision. **No AI is involved**; a test asserts the module does not even import the AI layer. Everything else gets a plain refusal that says general question-answering is separate, unscoped work.

### 3.2 FR-3 — the quantity suggestion

`app/initiatives/i13/quantity.py`. Pure arithmetic over a `WatchMetric` the caller already loaded:

```
target       = average monthly consumption × cover ceiling (12 months)
already have = stock on hand + open PO quantity
headroom     = target − already have
suggested    = min(floor(headroom), requested_quantity)     # never more than asked
```

Rules that matter:

- **Open PO quantity is netted off.** A requester who cannot see three already on order is exactly the person who orders a fourth.
- **Capped at what was asked.** This advises a reservation; Initiative 07 owns reorder points, and this must not quietly become a second, disagreeing implementation of one.
- **Rounded DOWN**, whole units. The ceiling is a ceiling. Known simplification: no UoM-aware rounding, so a material issued in metres cannot be suggested fractionally.
- **Below 3 consumption events in the look-back, no suggestion is made at all.** `suggested_quantity` is then `null` — **not zero**. "We suggest nothing" and "we suggest none" are opposite instructions.
- Every suggestion carries its own basis: cover ceiling, look-back, minimum history, the inputs, and both projected covers, plus an explicit note that all three configured values are **ours**, not VZI's (open question 10).

### 3.3 STITCH — the utilisation ledger (FR-5)

Two layers, the second built on the first, never rebuilding it.

**W6.1 `procurement_chain.py` — PR → PO → GR → GI, PO-item-anchored.** `EBAN → EKPO → EKBE → MSEG`, keyed on `EKPO.purchase_requisition`/`item_of_requisition` (BANFN/BNFPO). Quantity reconciliation clamps over-receipts. A PR item legitimately split across several POs is a real relationship, not a duplicate — so `(pr_number, pr_item)` is **not** a unique key; only `ledger_id` is. Every broken link carries a status and a reason (`NO_PR_REFERENCE`, `PR_REFERENCE_UNRESOLVED`, `NO_RECEIPTS`, `UNRESOLVED_PENDING_RESERVATION`) rather than being guessed — exactly what D9 requires.

**W6.2 `reservation_ledger.py` — + the reservation leg, OAR-scoped.** Anchored on `(reservation_number, reservation_item)`, never material+plant. Two new deterministic joins only:

- Reservation → PR: `raw_resb.purchase_requisition` / `item_of_requisition`, exact match.
- Reservation → GI: `raw_mseg.reservation` / `item_no_stock_transfer_reserv`, exact match. This is the link W6.1 did not have, and it supersedes W6.1's unresolved GI status wherever a reservation resolves. Issued quantity is then split into `procurement_issued_quantity` vs `direct_store_issued_quantity` — an arithmetic split of two independently deterministic quantities, populated only when both are known.

MRP consolidation (several reservations → one PR) is real but rare (2 PR items). No approved allocation rule exists, so those are marked `CONSOLIDATION_UNRESOLVED`: the PR link is shown because it is real, and the quantity fields are left empty rather than split by guesswork.

**`ledger_compat.py`** serves the older `/api/i13/ledger` shape that predates the Postgres migration — a different grain and different fields, kept because the frontend contract still uses it.

**Gap against FR-5:** the FRS names four deterministic keys — **RSNUM, AUFNR, BANFN, EBELN**. Only three are used. `RESB.AUFNR` is read and carried into attribution but **is not a stitching key**. It is populated on 39.2% of reservations against BANFN's 1.2%, so building the AUFNR leg is roughly a 30× improvement in reservation linkage and needs no SAP change.

### 3.4 WATCH — the utilisation mart (FR-1, FR-6)

**W3.5 `movement_metrics.py`** computes aging from goods movements directly, with zero runtime dependency on S031/S032:

- `last_movement_date` / `days_since_last_movement` — any qualifying movement.
- `last_issue_date` / `days_since_last_issue` — **goods issues only**, and this is what the band is classified on. A part last touched by a *receipt* is not FAST merely because something happened to it.
- Bands (`aging.py`): `≤365` FAST, `366–730` SLOW, `>730` NON_MOVING, **no history at all → NON_MOVING**. Thresholds are config, validated at startup (fast must be < slow).
- `consumption_count_12m`, `consumption_qty_12m`, and `inventory_turns = consumption_qty_12m ÷ current_stock`. Turns is `null` with reason `INSUFFICIENT_HISTORY` when stock is zero or unknown — the formula is hardcoded and the FRS never defines the convention, so this is an open question to VZI finance.

**W6.3 `watch.py`** composes W3.5 + W6.1 + W6.2 per material-plant and adds nothing it can reuse:

| Metric | How |
|---|---|
| `stock_on_hand` | `raw_mard.unrestricted`, summed per material+plant |
| `open_po_quantity` | `max(ordered − received, 0)` over W6.1's chain; PR-only rows excluded |
| `average_monthly_consumption` | `consumed_qty_12m ÷ 12` |
| `months_of_cover` | `stock ÷ rate`, **`null` with reason `INSUFFICIENT_HISTORY` when the rate is zero** — undefined, never 0 and never infinity |
| `projected_months_of_cover` | `(stock + open PO) ÷ rate` |
| `gr_not_issued_flag` | Any reservation entry where `received − issued > 0` and the oldest outstanding GR is ≥ 30 days old; carries days-since-GR, the relevant GR date, and received/issued/outstanding quantities |
| `acquired_vs_plan_status` | `NO_PLAN` / `BELOW_PLAN` / `ON_PLAN` / `ABOVE_PLAN` against summed planned quantity, plus variance quantity and percentage. **Exact comparison** — no tolerance band exists in configuration and none was invented |

WATCH itself is deliberately **not** OAR-scoped (every metric carries its own `material_scope` so callers filter without re-deriving the rule). `watch_mart.py` persists it to `i13_watch_metric_mart`, OAR-only by default, and `/api/i13/act/utilisation` reads that mart **without ever recalculating** — 0.26s, against ~12s for the live-compute `/watch`.

### 3.5 ACT — exceptions, confirmation, escalation (FR-7, FR-9, FR-11)

Two queues exist, and the distinction matters:

| | `/api/i13/exceptions` (W6.3) | `/api/i13/act/exceptions` (W6.6) |
|---|---|---|
| Persistence | None — recomputed per request | `i13_act_exception` |
| Owner | None | `owner_requester_id` |
| State machine | None | Validated transitions |
| Audit trail | None | `i13_act_exception_event`, append-only |
| Used by the UI | No (legacy, kept) | **Yes** — the Exceptions screen |

**Detection** (`act/detection.py`, pure — no I/O, no `datetime.now()`, everything passed in):

- **PLAN_BREACH** — `planned window end + grace(14d)` has passed and no goods issue has posted against the reservation. Any issued quantity counts; no partial-consumption threshold was invented. A plan with no date or with status ≠ `OPEN` cannot breach.
- **NO_PLAN** — an OAR reservation with no valid plan/session, sub-classified `MISSING_SESSION` / `INVALID_SESSION`. (`SESSION_WITHOUT_PLAN` is reserved and not producible from today's data.)
- **NO_PLAN_GRNI** — no plan *and* W6.3's GRNI evidence shows received stock unissued past the threshold. GRNI is **consumed**, never recomputed.
- **QUANTITY_OVERRIDE** — modelled, but depends on a quantity-decision source that this branch does not wire up.

**Dedup** is by deterministic business key (`ACT-PLAN_BREACH-{plan_id}`, `ACT-NO_PLAN-{rsnum}-{rspos}`, …), so a second detection run reuses the row rather than creating a duplicate, and a `RESOLVED` exception is never reopened — only a genuinely new key creates a new exception.

**State machine** (`act/state_machine.py`) — an explicit transition table. No code path assigns a status without calling `validate_transition`:

```
OPEN ──► AWAITING_REQUESTER ──► CONFIRMED ──► RESOLVED
  │              │       └────► ESCALATED ──► RESOLVED
  └──────────────┴─────────────────────────► RESOLVED
RESOLVED is terminal.
```

A confirmation POST against an `OPEN` exception is correctly rejected with **409** — an exception nobody was asked to answer cannot be answered.

**Escalation** — after `I13_REQUESTER_RESPONSE_DAYS` (default 5) with no confirmation, routed to the HOD from `I13_HOD_RECIPIENTS` (`"PLANT:identity,…"`). It is **empty by default**, so routing reports `ROUTING_PENDING` rather than inventing a recipient.

**Notifications** — `LoggingNotificationAdapter` only. The `NotificationPort` seam, channels (`PLATFORM_QUEUE`, `EMAIL`, matching D11) and the `LOGGED_ONLY` outcome all exist so "logged" is never reported as "delivered". **No adapter sends anything.**

**Cross-plant stock** (D10) is attached to every exception detail via `PostgresCrossPlantStockProvider` — informational only; nothing proposes or executes a transfer.

**Scheduling** — there is none. `POST /act/run/detect` and `POST /act/run/escalate` are manual triggers. A future Azure job would call the same two functions unchanged.

> ### The blocker in this package
>
> **42,649 exceptions exist. All are `OPEN`. Not one has an owner.** `POST /run/escalate` returns `escalated: 0, routing_pending: 0`.
>
> Every owner in `act/service.py` comes from `ConsumptionPlan.requester` (lines 350, 391, 421). For a **NO_PLAN** exception there is by definition no plan, so the owner is always `None`, the exception can never reach `AWAITING_REQUESTER`, and escalation has nothing to act on. All 42,649 are NO_PLAN.
>
> Meanwhile **W6.4 already resolves a requester for 80.6% of ledger entries** from `RESB.WEMPF`, and detection never consults it. This was named as the single highest-value fix on 21-Sep; **it is still not applied on this branch** (verified by reading `service.py` — no attribution import exists in `act/`). It is one integration point, needs no SAP change, no BAdI and no VZI decision, and would make ~80% of the queue routable.
>
> Caveat to state plainly: `WEMPF` is a goods-recipient *name*, not a verified identity. It unblocks demonstrability; delivering an actual email still needs the identity mapping.

### 3.6 Consumption attribution (W6.4, FR-4)

`consumption_attribution.py` resolves who owns a ledger entry's consumption, from the reservation line: requester (`WEMPF`), order (`AUFNR`), cost centre (gated). Disagreeing values across a business key are reported `AMBIGUOUS` with the conflicting candidates in `evidence` — the FRS forbids picking one.

Cost-centre enrichment sits behind `I13_COST_CENTRE_ATTRIBUTION_ENABLED`, **off**, because no EKKN/AUFK source is loaded — exactly the FRS §7 fallback. Reservation/requester/order attribution is never gated by that flag.

Measured across 92,684 ledger entries: `ATTRIBUTED` 17,148 · `PARTIALLY_ATTRIBUTED` 75,536 · **with a requester: 74,687 (80.6%)** · with an order: 32,339 (34.9%).

The `i13_consumption_attribution` mart is written by `refresh_consumption_attribution` and **read by no endpoint** — `/api/i13/consumption-attribution` computes live on every call.

### 3.7 Reclassification evidence (W6.5, FR-8)

`reclassification.py` applies SOP 3.1.1's three indicators, OR'd, at `(material, plant)` grain:

1. **FREQUENT_CONSUMPTION** — `consumption_count_12m > 4` (strict `>`, i.e. effectively ≥5), configurable.
2. **CRITICAL** — criticality tier ∈ `I13_RECLASS_CRITICAL_TIERS`, read through the shared W3.4 port. Default is `CRITICAL` only; `IMPACT` is deliberately excluded because the FRS ("Critical **or significant production impact**") is broader than the Dev Plan ("Critical flag"), and broadening a rule is a business decision. **This is a live discrepancy between the two documents.**
3. **HOD_JUSTIFIED** — from `HodJustificationProvider`, currently `NullHodJustificationProvider`.

**Nothing is fabricated.** An unavailable indicator returns `null`, never `False`, and `data_available` stays `False` so a partial answer is never mistaken for proven negative evidence. Advisory only — I13 never changes an MRP type, never computes ROP/Max, never calls into I07.

Today only indicator 1 can fire. **483 candidates are flagged on consumption frequency alone.** Indicator 3 is circularly blocked on W6.6's confirmation workflow, which cannot produce records until the owner gap above is closed.

### 3.8 Validation / reconciliation (FR-6)

`/api/i13/validation` reconciles computed counts against caller-supplied reference counts, with a configurable tolerance (`I13_RECONCILIATION_TOLERANCE_PCT=5.0`, our default — VZI has not confirmed one). Reconciles against W6.1's **PO/PR-item-grain** procurement chain, matching ZMM065's own grain, not W6.2's reservation-anchored ledger.

No ZMM065 export or 30-Day GR Report is loaded, so both sources return `REFERENCE_UNAVAILABLE` unless counts are typed in by hand. Computed side works: 255,916 procurement entries, 179 GRNI.

---

## 4. Backend API inventory

Base prefix `/api`. Everything is `GET` unless marked. **No endpoint writes to SAP** — asserted app-wide by `tests/test_write_paths.py`, which requires every non-GET route to be listed with a sentence saying what it writes and why.

### 4.1 Summary and diagnostics

| Endpoint | Returns | Reads |
|---|---|---|
| `/i13/summary` | 9 dashboard counts: total OAR positions, fast/slow/non-moving, GRNI-30d, plan breaches, no-plan, reclassification candidates, `valuation_is_mocked` | OAR scope from `raw_marc`, W3.5 over all movements, the full exception chain, reclassification. **~4 minutes unfiltered** — all 233k movement rows are pulled into Python and aggregated there |
| `/i13/data-sources` | Per-table load status from `ingestion_run` | `ingestion_run` only |

### 4.2 WATCH and movement metrics

| Endpoint | Params | Notes |
|---|---|---|
| `/i13/watch` | `plant`, `material`, `aging_band` | Live-computes the whole population. **No `limit`/`offset`** — an unfiltered call returns ~13 MB in ~12s |
| `/i13/movement-metrics` | `plant`, `material`, `aging_band`, `limit`, `offset` | W3.5 only |
| `/i13/materials/{material}/plants/{plant}/movement-metrics` | — | 404 when no movement history exists — a data gap, not a zero |

### 4.3 The ledger

| Endpoint | Params | Notes |
|---|---|---|
| `/i13/ledger` | `plant`, `material`, `include_out_of_scope`, `limit` (100/1000), `offset` | Legacy compat shape. Slicing happens **after** a full build — filter by `material`, don't page |
| `/i13/ledger/{ledger_id}` | `include_out_of_scope` | |
| `/i13/utilisation-ledger` | `material`, `plant`, `reservation_number`, `pr_number`, `lifecycle_status`, `include_out_of_scope`, `limit`, `offset` | W6.2, the complete chain. Each row is enriched with `attribution_status`/`attribution_evidence` |
| `/i13/utilisation-ledger/{rsnum}/{rspos}` | `include_out_of_scope` | |
| `/i13/utilisation-ledger/partial` | `material`, `plant`, `pr_number`, `po_number`, `lifecycle_status`, `limit`, `offset` | W6.1, no reservation leg. **Registered before** the two-segment parameterised route so `partial` is not swallowed by it |
| `/i13/utilisation-ledger/partial/diagnostics` | `material`, `plant` | Match rates and duplicate-key counts — FRS §13/§19.15 "reported, never discarded" |

### 4.4 Exceptions, reclassification, validation, attribution

| Endpoint | Params | Notes |
|---|---|---|
| `/i13/exceptions` | `plant`, `material`, `exception_type`, `status` | The **ephemeral** queue. `material`/`plant` push into real SQL filters; still double-digit seconds |
| `/i13/reclassification` | `plant`, `material` | SOP 3.1.1 evidence with per-candidate reasons |
| `/i13/validation` | `zmm065_reference_count`, `gr_30_day_reference_count` | Both optional; absent → `REFERENCE_UNAVAILABLE` |
| `/i13/consumption-attribution` | `material`, `plant`, `reservation_number`, `pr_number`, `include_out_of_scope`, `limit`, `offset` | Computes live; never touches the mart, so it has no side effects |
| `/i13/consumption-attribution/{rsnum}/{rspos}` | `include_out_of_scope` | |

### 4.5 ACT (`/i13/act/*`)

| Method | Endpoint | Notes |
|---|---|---|
| GET | `/act/utilisation` | Reads `i13_watch_metric_mart` only. Filters: `plant`, `material`, `aging_band`, `grni`, `acquired_vs_plan_status`. **No `limit`/`offset`** |
| GET | `/act/utilisation/{material}/{plant}` | 404 → "run the W6.3 refresh first" |
| GET | `/act/exceptions` | `plant`, `material`, `type`, `status`, `owner_requester_id`. **No `limit`/`offset`** |
| GET | `/act/exceptions/{id}` | + cross-plant stock + the confirmation if one exists |
| GET | `/act/exceptions/{id}/history` | Append-only event trail |
| POST | `/act/exceptions/{id}/confirmation` | The **only** way a confirmation is recorded. No endpoint sets `status` directly. 404 unknown · 409 invalid transition |
| POST | `/act/run/detect` | Body: `as_of_time`, `material`, `plant`. Returns created/reused/resolved/routed. Idempotent |
| POST | `/act/run/escalate` | Body: `as_of_time`. Returns escalated/routing-pending counts |

### 4.6 WS7 — under `/i13`

| Method | Endpoint | Notes |
|---|---|---|
| POST | `/i13/consumption-plans` | FR-4 capture. **Requires a valid session.** Validates purpose non-empty, window_end ≥ window_start, quantity > 0 (as a *string*, so no float round-trip). Author from `X-Actor-Id`, never the body. Status written as `"OPEN"` — the vocabulary detection actually checks |
| GET | `/i13/consumption-plans` | `session_id`, `material`, `plant`, `limit`. **Captured plans only** — deliberately not merged with the 742 CSV rows |
| GET | `/i13/quantity-suggestion` | `material`, `plant`, `quantity`. FR-3 standalone. Reads WATCH exactly as `/i13/watch` does, so the suggestion and the WATCH screen cannot disagree. 404 when no WATCH row exists — "a data gap, not a stock position of zero" |

> **These two are camelCase** (pydantic alias generator). Every other `/api/i13/*` route is snake_case.

### 4.7 Shared assistant

| Method | Endpoint | Notes |
|---|---|---|
| POST | `/assistant/sessions` | Body: `materialId`, `plant`, `quantity?`, `origin` (`BADI`\|`PLATFORM`). Returns routing always, session+step only when a flow matched. **No flow is a 200 with a null session, not a 404** |
| POST | `/assistant/sessions/{id}/turns` | Answer the current step, get the next |
| GET | `/assistant/sessions/{id}` | The FR-8 trace: routing reason, assessment **replayed from stored JSON not recomputed**, every turn, plans, suggestions, justifications, and `linkageNote` — served on every trace, because B2 is a known gap in what the platform can prove, not a per-session discovery |
| GET | `/assistant/sessions` | `flow`, `material`, `plant`, `limit`. Outcome derived from turns |
| POST | `/assistant/ask` | Fixed intents. **Never 4xx for an unrecognised question** — "I do not know" is a valid answer and a 422 would render a failure where the honest response is a sentence |
| GET | `/assistant/ask/suggestions` | The chips, served so the UI cannot offer a question the backend stopped answering |
| POST/GET | `/justifications` | `session_id`, `exception_id`, `kind`, `material`, `plant`, `limit`. Reason category validated against **configuration, not an enum**, because VZI's vocabulary has not arrived and an enum would need a migration on the day it does |

### 4.8 Identity

`get_current_actor` reads an `X-Actor-Id` header and trusts it. **No authentication exists anywhere in this codebase.** Reading a header rather than a body field means identity is at least not whatever an arbitrary JSON payload claims. When Entra lands, this one dependency changes and every caller stays the same. Sessions today are issued to `UNAUTHENTICATED_LOCAL_USER` — visible on purpose rather than hidden behind a blank field.

---

## 5. Configuration

All in `app/core/config.py`, surfaced as typed objects by `app/initiatives/i13/config.py`. Values marked **ours** are defaults this team chose because the FRS names them as configuration and supplies no number.

| Setting | Default | Source |
|---|---|---|
| `I13_OAR_MRP_TYPES` | `ND,PD` | VZI ruling 08-Sep |
| `I13_MIN_MAX_MRP_TYPES` | `VB` | VZI ruling 08-Sep |
| `I13_AGING_FAST_MAX_DAYS` / `SLOW_MAX_DAYS` | `365` / `730` | SOP 3.15 |
| `I13_CONSUMPTION_WINDOW_MONTHS` | `12` | FRS |
| `I13_GR_NOT_ISSUED_THRESHOLD_DAYS` | `30` | FRS |
| `I13_PLAN_BREACH_GRACE_DAYS` | `14` | **ours** |
| `I13_RECLASS_MIN_CONSUMPTION_COUNT` | `4` | SOP 3.1.1 |
| `I13_RECLASS_CRITICAL_TIERS` | `CRITICAL` | **ours** — FRS/Dev Plan disagree |
| `I13_RECONCILIATION_TOLERANCE_PCT` | `5.0` | **ours** |
| `I13_COST_CENTRE_ATTRIBUTION_ENABLED` | `false` | No EKKN/AUFK source |
| `I13_REQUESTER_RESPONSE_DAYS` | `5` | **ours** |
| `I13_HOD_RECIPIENTS` | `""` | Empty by design → routing `PENDING` |
| `I13_QUANTITY_COVER_CEILING_MONTHS` | `12.0` | **ours** |
| `I13_QUANTITY_LOOKBACK_MONTHS` | `12` | **ours** — deliberately separate from the WATCH window |
| `I13_QUANTITY_MIN_HISTORY_CONSUMPTIONS` | `3` | **ours** |
| `ASSISTANT_SESSION_ID_LENGTH` / `_PREFIX` | `10` / `S` | Sized to `Bednr` |
| `ASSISTANT_SESSION_TTL_HOURS` | `72` | **ours** — reported, never enforced |
| `ASSISTANT_NARRATIVE_ENABLED` | `false` | Needs sign-off |
| `ASSISTANT_JUSTIFICATION_REASON_CATEGORIES` | 7 placeholders | **ours** — awaiting VZI |
| `ASSISTANT_FREE_TEXT_INTENTS_ENABLED` | `true` | |

Plant scope is **not** configuration — `IN_SCOPE_PLANTS = ("1300", "1500")` is a constant in `app/shared/plant_scope.py`, deliberately immutable so nobody widens scope by accident.

---

## 6. The dashboards

Next.js App Router. Every OAR screen is an **async server component** that fetches once on the server through a loader in `features/initiative-13/data/live-loaders.ts`, then hands rows to a `"use client"` table that filters them. Filters live in the URL, so a filtered view is a link. Writes go through Server Actions so `revalidatePath` re-renders every affected screen — against the previous client-side pages a write could not update anything, because the rows lived in `useState`.

**Every loader throws on failure, and the page renders that visibly.** An empty table and an unreachable backend are different statements and one must never be shown as the other.

**Row cap:** the I13 routes return bare arrays with no envelope and no total, so loaders ask for `ROW_CAP = 1000` and report `atLimit` when the response comes back exactly full — rendered as "the first 1,000", never "1,000 of N".

### 6.1 Nav — `/oar-utilization`

| Screen | Route | Backend | Status |
|---|---|---|---|
| Overview | `/oar-utilization` | `/i13/summary` + `/i13/act/utilisation` | **Live + 3 demo charts**, banner-disclosed |
| Utilisation Dashboard | `…/utilisation-dashboard` | 7 sections, `Promise.allSettled` | **Live**, sections degrade independently |
| Utilization Ledger | `…/ledger` | `/i13/ledger` | **Live only**, no fixture fallback |
| Exceptions | `…/aging-exceptions` | `/i13/act/exceptions` + `/assistant/sessions` + `/justifications` | **Live**, with the confirmation write path |
| Consumption Plans | `…/plans` | `/i13/consumption-plans` | **Live** — captured plans only |
| WATCH | `…/watch` | `/i13/act/utilisation` + `/assistant/sessions` | **Live** (mart) |
| 30-Day GR-Not-Issued | `…/gr-not-issued` | — | **Demo data**, banner-disclosed |
| Consumption / Usage Pattern | `…/usage-patterns` | — | **Demo data**, banner-disclosed |
| Redeployment | `…/redeployment` | — | **Demo data**, banner-disclosed |
| Reclassification | `…/reclassification` | `/i13/reclassification` | **Live** |
| Validation | `…/validation` | `/i13/validation` | **Live** |

### 6.2 What each live screen shows

**Overview** — eight KPI tiles straight off `/i13/summary` (total OAR positions, fast/slow/non-moving, GRNI-30d, plan breaches, no-plan, reclassification candidates), plus aging-band and acquired-vs-plan distributions **counted from the rows the backend classified** — nothing is re-derived in the browser. Three charts (unutilized value by department, NM/SM inflow trend, redeployment avoidance) are hand-written illustrations; there is no valuation source in I13's table set at all, and a warning banner says so.

**Utilisation Dashboard (FR-10)** — the consolidated view: KPIs, aging distribution, acquired-vs-plan panel, non-mover drilldown (joined to the reclassification mart for the critical-impact indicator, showing **Unknown** rather than **No** when unavailable), exception-status panel, captured plans, justification log, validation, and a collapsed data-source diagnostic. Its one *official* dependency is WATCH; reclassification, ACT, plans and validation each settle independently and a 404 renders as "not available" rather than an error. Transforms are grouping, joining and filtering only — never a business rule. A staleness note renders the newest `calculatedAt`, because nothing recomputes on its own.

**Utilization Ledger (FR-5)** — the reservation → PR → PO → GR → GI chain with linkage counts per state, which is how FRS AC-3's "unmatched records reported rather than inferred" is surfaced.

**Exceptions (FR-9)** — the ACT queue, joined to the assistant session log so a row can link to the conversation behind it (session fetched **once** for the screen, not per row). The header states the **unrouted count**, because an exception with no owner routes to nobody, escalates to nobody, and sits in the queue looking like work while being inert. The Acknowledge/Resolve buttons that used to move a badge in local state are replaced by `confirmExceptionAction`, a real validated transition with an audit entry. A 422/409 is passed through verbatim — usually the routing gap talking, and the message says so better than a generic failure.

**WATCH (FR-1/FR-6)** — the persisted mart per material+plant, with `calculatedAt` staleness.

**Consumption Plans (FR-4)** — the screen I13 did not have. Its point is a single number: **how many plans are real**. The dashboard's acquired-vs-plan engine is genuine and 742 of its inputs came from a generator; this list carries only `CAPTURED` rows. Expect it to be small — that is the measure of assistant adoption, not something to soften.

**Reclassification (FR-8)** — SOP indicators with supporting counts, rendering `null` as **Unknown**.

**Validation (FR-6)** — reference counts are typed in and passed straight through as query parameters; **no reconciliation math runs in the browser**, on the one screen whose entire purpose is saying whether two numbers agree.

### 6.3 Assistant screens

| Route | Purpose |
|---|---|
| `/assistant` | Landing page + the fixed-intent free-text box |
| `/assistant/new` | **The deep-link the SAP BAdI pop-up will open.** Contract: `?material={MATNR}&plant={WERKS}&origin=BADI` |
| `/assistant/sessions` | The session log with a compliance panel |
| `/assistant/sessions/[sessionId]` | The FR-8 trace: advice as served, turns, plan, suggestion, justifications |

Both assistant flows share one UI (`assistant-workspace`, `step-renderer`, `step-form`, `assessment-card`, `session-reference`, `compliance-panel`), because the backend decides what to ask next and the frontend only renders it — the script exists once, in Python, and cannot drift into a second TypeScript copy that disagrees about a compliance question in front of a user.

### 6.4 Write paths from the UI

There are exactly two, both Server Actions in `features/initiative-13/actions.ts`, and **neither retries**. Every table behind them is append-only, so a retry after a timeout that actually succeeded writes a second row nobody can remove.

- `confirmExceptionAction` → `POST /i13/act/exceptions/{id}/confirmation`, then `revalidatePath("/oar-utilization", "layout")`.
- `runDetectionAction` → `POST /i13/act/run/detect`. **The only thing that moves any number on these screens.** FR-6 asks for a daily refresh and there is no scheduler, so a refresh is this button.

---

## 7. Contract drift between frontend and backend

Four mismatches found by reading both sides against each other. All four are real on the currently checked-out branch pair.

| # | Where | What happens |
|---|---|---|
| 1 | `/i13/quantity-suggestion/compute` | The client (`computeI13QuantitySuggestion`) calls `/compute`; the backend only serves `/i13/quantity-suggestion`. **404.** Currently unreferenced by any page, so it is latent — but it is a documented API in the client and will fail the first time a screen uses it |
| 2 | `/i13/data-sources` | The adapter reads `entity_set`, `mode`, `available`, `fetched_at`; the backend now returns `table`, `row_count`, `status`, `loaded_at`. The panel renders **blank names, 0 rows and a red `UNAVAILABLE` badge for every table** regardless of the real state. Used on the Ledger and Utilisation Dashboard screens |
| 3 | `limit`/`offset` on `/act/utilisation` and `/act/exceptions` | Sent by the client, **not declared** on either route, so FastAPI ignores them. The exceptions board therefore fetches the full queue — **42,649 rows** — unpaged, and `atLimit` never fires |
| 4 | `department` / `requestedFor` | The frontend's newer WS7 commits send them on `POST /assistant/sessions` and expect them back on the trace. **Neither field exists anywhere in the backend.** They are silently dropped on the way in and come back `undefined` |

Separately: the backend still accepts `quantity` on `POST /assistant/sessions`, which the frontend has stopped sending (commit `56cc783`). Harmless — the FR-3 suggestion is computed against the *planned* quantity from the capture form, which is the fresher statement of intent — but the assessment's `requestedQuantity` will be null on every BAdI-opened session.

---

## 8. Branch divergence — two Initiative 13 lines of work

The checked-out backend branch is `feat/vp/ws7-assistant`. A parallel branch **`feat/sj/init13` carries two commits that are not on it**, and they are substantial:

| Commit | Brings |
|---|---|
| `636d0d2` Quantity suggestion implementation | A **second, persisted** W7.4 engine: `app/initiatives/i13/quantity_suggestion{,_reason,_store}.py`, `app/api/i13/quantity_suggestion.py`, `app/models/i13_quantity_suggestion.py`, a migration, `app/prompts/i13_quantity_suggestion/v1.md`, and **five endpoints** — `GET/POST /i13/quantity-suggestion`, `GET /{id}`, `POST /{id}/acceptance`, `POST /{id}/justification` |
| `6d8e7cf` Performance-related changes | SQL-side filtering and paging (`app/integrations/sap/sql_filters.py`, `_request_cache.py`), `?live=true` on `/summary` and `/reclassification`, and **`python -m app.refresh_marts`** — the committed CLI that refreshes all three marts |

Also only on that branch: `Initiative_13_FRS_v1.2_final.docx`, `I13_Performance_Remediation_Plan_22_Sep_2026.md`, `API_Inventory_and_Data_Lineage_23_Sep_2026.md`, and W7.4's implementation plan.

**Two consequences to resolve before merging.**

1. **There are two FR-3 implementations.** `feat/vp/ws7-assistant` has `quantity.py` — pure, in-memory, consumed by the conversation and by `GET /i13/quantity-suggestion`. `feat/sj/init13` has `quantity_suggestion*.py` — persisted, with acceptance and justification endpoints and its own tables. They **collide on the same URL**, and drift #1 in §7 is the frontend already written against the second one. Someone has to pick.
2. **`app/refresh_marts/` exists on disk as `__pycache__` only** in the current working tree — the source is on `feat/sj/init13`. So on the checked-out branch there is **no committed way to refresh a mart outside the test suite**, which also means `pytest` is currently the thing that overwrites them.

The performance numbers in §9 below come from the `feat/sj/init13` measurements; the checked-out branch does not have the SQL-side filtering, so `/watch` and `/exceptions` are slower there than the table shows.

---

## 9. Performance

Measured 23-Sep on `feat/sj/init13`:

| Endpoint | Unfiltered | Filtered |
|---|---:|---:|
| `/act/utilisation` | 0.28s | mart-backed, always fast |
| `/reclassification` | 0.78s | mart-backed, pages in SQL |
| `/watch` | ~12s | `?limit=50` → 0.43s |
| `/exceptions` | ~14–18s | `?limit=100` → 0.15s |
| `/summary` | ~14–18s (≈4 min without the remediation) | no filter available |

On the checked-out branch, `/i13/summary` pulls all 233,145 movement rows into Python and aggregates there — **~4 minutes**, and the reason the Postgres test suite takes ~17 minutes. `/ledger`, `/utilisation-ledger*` and `/consumption-attribution` still build everything and then slice, so their `limit` bounds payload only, not work.

---

## 10. FRS v1.1 traceability

| FR | Requirement | State |
|---|---|---|
| **FR-1** | OAR demand context per material+plant | ✅ Built. Stock, open POs, 12M consumption, months of cover, days since last movement, aging class, criticality. ⚠️ **MCHB batch stock is not read**, so stock is understated for batch-managed materials |
| **FR-2** | Reservation-time assistant | ✅ Built and running. (a) cross-check incl. other plants, (b) plan capture with a **window**, (c) suggestion + reason, (d) override justification, (e) session identifier. ⚠️ Not BAdI-triggered — the deep-link contract is published, the BAdI is not built. ⚠️ LLM narrative off by default |
| **FR-3** | Quantity suggestion | ✅ Built. Months-of-cover basis, net of stock and open POs, configurable ceiling/look-back/minimum, no suggestion below minimum history. ⚠️ **EKET is not read**, so open-PO netting uses EKPO/EKBE rather than schedule lines as specified. ⚠️ Two implementations across branches |
| **FR-4** | Plan record + session traceability | ⚠️ **Half built.** The plan is stored against session/requester/material/plant/date. **Reading the session identifier back off the reservation is impossible** — no field is designated and `Bednr`/`ZZAISESSION` is not exposed. A captured plan carries no reservation number, so it cannot yet clear a NO_PLAN exception. This is blocker B2 |
| **FR-5** | STITCH ledger | ⚠️ Built, **3 of 4 keys**. RSNUM, BANFN, EBELN used; **AUFNR not used as a stitching key**. Deterministic only, unmatched reported. Data limits it: 1.2% of reservations carry a PR reference |
| **FR-6** | WATCH metrics | ⚠️ Built. All metrics computed and persisted. **No daily refresh** — no scheduler exists. **No reconciliation reference data.** Aging bands are invalidated by history depth (§11) |
| **FR-7** | Plan-breach / no-plan detection | ✅ Built, with grace period and GRNI fallback, and a justification prompt per exception. ⚠️ Fires only against fabricated plans today. ⚠️ Detection reads the plan's *start* date, not `window end + grace` — a deliberate deferral, since widening it changes when breaches fire and needs its own tests |
| **FR-8** | Reclassification evidence | ⚠️ Built. **1 of 3 indicators can fire**; the other two return `null`, never `false` |
| **FR-9** | ACT queue and escalation | ⚠️ Machinery complete — state machine, audit trail, structured confirmation, idempotent detection, cross-plant stock, configurable escalation. **Zero exceptions routed.** See §3.5 |
| **FR-10** | Dashboard and reporting | ✅ Built. 11 screens, 7 of them live, export on the non-mover drilldown, validation views. Demo-backed screens carry warning banners |
| **FR-11** | Notifications | ❌ **Not delivered.** Port, channels and audit outcomes exist; `LoggingNotificationAdapter` is the only implementation and nothing is sent |

### Acceptance criteria (FRS §9)

| # | Criterion | Verdict |
|---|---|---|
| 1 | Assistant returns cross-check, captures a plan, suggests a quantity, captures a justification, issues a session ID | ✅ **Met** |
| 2 | Every OAR reservation with a valid session ID links to its plan and chain; every one without raises NO_PLAN | ⚠️ **Half.** NO_PLAN raises correctly. The forward link cannot be read back — B2 |
| 3 | Deterministic stitching for every OAR acquisition in the 12-month window, unmatched reported | ⚠️ **Reporting met, coverage not.** 1.2% reservation→PR, 37.6% PO→PR. Not closable by build effort |
| 4 | Aging band, cover, days since movement, turns, GRNI per OAR position, refreshed daily and reconciled monthly | ❌ **No daily refresh, no reference data.** Metrics all computed |
| 5 | Breaches raised within one refresh cycle | ⚠️ Detection is correct and idempotent; there is no cycle to be within |
| 6 | Every exception routes to the requester and escalates after the configured period | ❌ **0 of 42,649 routed** |
| 7 | Reclassification applies the SOP indicators exactly, with supporting counts | ⚠️ Counts shown; 2 of 3 indicators unavailable |
| 8 | **Zero SAP write-back events logged** | ✅ **Met**, and structurally guaranteed by `tests/test_write_paths.py` |
| 9 | Exception-queue items and emails generated | ⚠️ Queue items yes. **No emails** |

---

## 11. Blockers, ranked

### B0 — ACT exception owner is never resolved *(ours, one integration point)*
42,649 exceptions, 0 owners, 0 routed, 0 escalatable. Detection reads `ConsumptionPlan.requester`; a NO_PLAN exception has no plan by definition. W6.4 already resolves a requester for 80.6% of ledger entries and detection never consults it. **No SAP change, no BAdI, no VZI decision required. Highest value available and still not applied.**

### B1 — Reservation → PR linkage is 1.2% *(data)*
1,274 of 105,848 reservations carry a PR reference. In standard SAP, RESB does not normally carry one — linkage runs through the order (AUFNR) or the account assignment. **Interim mitigation in code: build the AUFNR leg** (39.2% coverage, already named in FR-5, data already extracted). Needs a VZI/SAP functional answer on how the link is actually made.

### B2 — No session field on the reservation *(SAP)*
The single field that joins a platform plan to a SAP reservation. `RESB.item_text_line_1` is **empty across all 105,848 rows** and is a clean candidate; `RESB.text` is already 24.4% populated and is not. Until this lands, FR-4's second half cannot be built — and a test asserts the reservation number stays empty, so it will fail on the day it does land, which is the right moment to extend the end-to-end proof.

### B3 — MARC covers one of two in-scope plants *(data)*
| Plant | MARC rows | MSEG | RESB | EKPO |
|---|---:|---:|---:|---:|
| 1300 Black Mountain | 45,352 | 99,394 | 92,673 | 45,797 |
| **1500 Gamsberg** | **0** | **122,868** | 10,333 | 35,083 |

Gamsberg has *more* goods movements than Black Mountain and no MARC rows at all, so every material there classifies `EXCLUDED` and is invisible to OAR scope, WATCH, exceptions and reclassification. **Half the platform is dark.**

### B4 — Goods-movement history is 12.5 months against the 731 days the bands need *(data — FRS v1.2 §7.2)*
MKPF spans 08-Aug-2025 to 20-Aug-2026 (≈409 days). The SOP bands need 731 days to separate SLOW (366–730) from NON_MOVING (>730). Any material whose last issue predates the window has no row at all and defaults to NON_MOVING, and the SLOW band is observable only across a 43-day sliver. **This is visible in the output: 40,868 NON_MOVING · 3,148 FAST · 378 SLOW.** Re-extract MSEG and MKPF over at least 25 months before any aging figure is used for a business decision.

### B5 — Every consumption plan is fabricated *(ours + B2)*
742 orphaned rows, 0 joining real data. `acquired_vs_plan_status` is `NO_PLAN` for 100% of real material-plants, and `plan_breach_count: 313` on `/summary` is a **false KPI derived entirely from them**. PLAN_BREACH detection has never run against a valid plan→reservation join. Real plans can now only come from the assistant — and can only *join* once B2 lands. Recommended: delete the file so the false KPI disappears, and hand-seed a small valid plan set to exercise the breach path before UAT.

### B6 — WATCH mart covers 16% of the OAR population
7,184 rows against 44,394 OAR positions, plant 1300 only. Partly B3, partly because refresh has only ever run scoped — and on the checked-out branch there is no committed refresh CLI.

### B7 — No reconciliation reference data
Neither ZMM065 nor the 30-Day GR Report is loaded, so FR-6 acceptance cannot be closed.

### B8 — `data-sources` under-reports MSEG by 56%
`raw_mseg` loaded in two batches with identical timestamps; the query takes `ORDER BY finished_at DESC LIMIT 1` and reports 102,631 against an actual 233,145. Also affects `raw_cdhdr`, `raw_s031`, `raw_s032`. Fix: sum all successful runs per table. Compounded by drift #2 in §7, which means the panel shows nothing useful at all today.

### B9 — Infrastructure
Azure SQL is unreachable outside the VNet; ADLS requires an `abfss://` URL and is not working, so `python -m app.seed` cannot run on Azure; `psycopg` is missing from `requirements.txt`; Entra SSO is declared in config and read by no code. **Local Postgres is the only working data source today.**

---

## 12. FRS corrections needed for v1.2/v1.3

Five statements in v1.1 are now wrong. Three are already fixed in `Initiative_13_FRS_v1.2_final.docx` (on `feat/sj/init13`); two are not.

| v1.1 says | Reality | In v1.2? |
|---|---|---|
| §3.1/§8/§10: **BLOCKING** — MARA.EXTWG not exposed, the entire I13 scope depends on it | Superseded by the 08-Sep DISMM ruling. EXTWG read nowhere. 44,394 OAR positions selected | ✅ Corrected |
| §3.1/§7: S031/S032 are the system-derived aging source | W3.5 computes aging from goods movements. S031 (54,406 rows) and S032 (20,512) are loaded and read by nothing | ✅ Corrected |
| §7: EKBE is a "proposed addition carried open" | Already extracted (157,266 rows) and a hard dependency of FR-6 | ✅ Corrected |
| §3.1/§8: four in-scope plants | The platform serves **two**, 1300 and 1500, by the 21-Sep ruling. The four codes are still unconfirmed | ⚠️ Flagged, not resolved |
| §8: dev client holds ~2,034 materials, supports mechanics validation only | 45,352 distinct materials loaded. The calibration deferral may no longer be justified — **confirm with VZI that this extract is production-representative** | ⚠️ Open |

---

## 13. Open questions, by owner

**VZI / SAP functional — blocking**
1. How does an OAR reservation actually link to its PR/PO? (`BANFN` 1.2%, `AUFNR` 39.2%)
2. Which four plant codes are in scope, and can MARC be extended to cover them — 1500 at minimum?
3. Can MSEG/MKPF be re-extracted over at least 25 months? The aging bands are not usable until then.
4. Is `WEMPF` (goods recipient) an acceptable requester proxy for accountability and routing?

**SAP team**
5. Designate the session-ID field — is `RESB.item_text_line_1` (empty on all 105,848 rows) available, or is a Z-append preferred?
6. Reservation-entry BAdI: confirm the ABAP stream and transport timing.
7. Confirm EKKN/AUFK exposure, or confirm `RESB.g_l_account` (38.4%) as the cost-object substitute. *(EKBE can be closed.)*
8. Expose EKET for FR-3's open-PO netting, and MAKT beyond its current 8.2% coverage for FR-10 labels.

**VZI business / SOP owner**
9. Requester response period before HOD escalation (default 5 days).
10. HOD-per-plant (DOA) mapping.
11. Inventory-turns convention — numerator, denominator, averaging period.
12. Reconciliation tolerance against ZMM065 / the 30-Day GR Report (default 5%).
13. Acquired-vs-plan: exact comparison, or a tolerance band?
14. Criticality tiers counting as "Critical" — `CRITICAL` only, or `CRITICAL` + `IMPACT`? *(FRS and Dev Plan differ.)*
15. Is 92% NON_MOVING expected — noting B4 makes the current figure unreliable either way?
16. Justification reason categories — the seven configured are placeholders.
17. FR-3's three values: cover ceiling, look-back, minimum history.
18. Session validity window (defaulted to 72h, reported and never enforced — treating an expired reference as non-compliant would raise an exception nobody could clear).
19. **Is the OAR rule right?** ND+PD selects 97.8% of the catalogue, so the assistant will ask for a plan on almost every part.
20. **Does I08 beating I13 match business intent** when a part is both? It applies to 97.6% of repairables.

**Internal / architecture**
21. Sign-off on the deterministic-core deviation before the LLM narrative is switched on.
22. Where does the daily refresh run — Azure Function, WebJob, or Data Factory trigger?
23. Which W7.4 quantity-suggestion implementation survives the merge (§8)?

---

## 14. Suggested order of work

**Immediate — no external dependency, high payoff**
1. **Wire ACT's exception owner to W6.4 attribution.** Unblocks ~80% of the queue and makes confirm/escalate demonstrable. Single highest-value change available.
2. **Resolve the branch divergence** — pick one FR-3 implementation, land `app/refresh_marts`, land the SQL-side filtering.
3. **Fix the four frontend/backend drifts** in §7 — the `/compute` path, the data-source shape, `limit`/`offset` on the two ACT routes, and `department`/`requestedFor`.
4. **Delete the orphaned `consumption_plans.csv`** so the false `plan_breach_count` disappears.
5. **Build the AUFNR stitching leg** (FR-5, ~30× the reservation linkage).
6. **Fix `data-sources`** to sum all successful ingestion runs per table.

**Short term — engineering**
7. Full-population WATCH mart refresh, then schedule the daily job (FR-6).
8. Move the remaining Python-side aggregation into SQL.
9. Implement the email and platform-queue adapters behind the existing `NotificationPort`.
10. Read MCHB (FR-1 batch stock) and EKET (FR-3 open-PO netting).

**Dependent on VZI / SAP**
11. Resolve the reservation→procurement linkage question (B1).
12. Re-extract MSEG/MKPF over 25+ months (B4) — without this the aging output should not reach a business decision.
13. Extend MARC to the in-scope plants (B3).
14. Load ZMM065 and the 30-Day GR Report (B7), agree the tolerance, and close FR-6 acceptance.
15. Designate the session field (B2), then extend the end-to-end proof to show a captured plan clearing a NO_PLAN exception.
16. Entra SSO, then replace the `X-Actor-Id` header with real claims.

---

## 15. Things that will look odd in a demo, and are not broken

- **Every consumption plan on screen is fabricated** unless somebody captures one through the chat first. Say this out loud before demoing the exception queue: the engine is real, most of its input is not.
- **Every session is issued to `UNAUTHENTICATED_LOCAL_USER`.** Entra is not wired in, and this is visible on purpose rather than hidden behind a blank field.
- **The exception queue has no owners.** See B0.
- **Plant 1500 shows nothing.** See B3.
- **Almost everything is NON_MOVING.** See B4 — the window is too short for the band to mean what it says.
- **Months of cover is often blank.** That is `null` for "nothing is consumed, so cover is effectively unlimited" — not zero. Rendering it as zero would tell somebody to buy more of a part nothing consumes.
- **Reclassification shows "Unknown", not "No".** Two of three indicators have no source. The difference between "we checked and it is not critical" and "nothing here can tell us" decides whether the list is trustworthy.
- **Validation says `REFERENCE_UNAVAILABLE`.** A missing input, not a failure.
- **The assistant never says "the vendor has it."** Zero of 788 open repair lines carry a dispatch movement, so no line can be confirmed as physically with the vendor.

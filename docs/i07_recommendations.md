# I07 Recommendations, Approval Workflow & SAP Adoption (Phase 7)

Converts Phase 3–6 outputs into governed, explainable recommendations and
manages their lifecycle through human approval to manual SAP execution.

```
Phase 3 Feature Store
       ↓
Phase 4 Forecasting
       ↓
Phase 5 Inventory Calculations
       ↓
Phase 6 OAR Similarity
       ↓
PHASE 7: Recommendation
       ↓
Explanation + calculation trace
       ↓
OAR conversion eligibility (where applicable)
       ↓
Human approval (4-step chain)
       ↓
Approval ledger
       ↓
MANUAL VZI SAP TRANSACTION  ← outside this system
       ↓
Read-only SAP adoption evidence
       ↓
ADOPTED / PARTIALLY_ADOPTED / NOT_ADOPTED / UNKNOWN
```

> **AI recommends. Humans approve. SAP is updated manually. I07 does not
> automatically update SAP in Phase 7.** After final human approval, the
> approved change is executed manually in SAP using the standard VZI process.
> I07 only records execution evidence and/or reads SAP state for adoption
> reconciliation.

## Architecture

| Module | Responsibility |
| --- | --- |
| `repository.py` | bulk reads of the latest Phase 3/4/5/6 runs |
| `builder.py` | assembles one recommendation per material-plant — never recalculates a formula |
| `explanation.py` | deterministic reasons, calculation trace, expected impact (no LLM) |
| `conversion.py` | OAR → Min-Max trigger evaluation |
| `workflow.py` | the four-step approval state machine (pure functions) |
| `ledger.py` | applies workflow actions, writes the immutable audit trail |
| `execution.py` | manual SAP execution evidence capture — never a SAP call |
| `adoption.py` | read-only SAP state reconciliation, through an interface |
| `service.py` | batch recommendation generation |

Every module reuses Phase 1's `Recommendation` contract, `PolicyDocument`,
`ConversionTriggerPolicy` and `AdoptionPolicy` rather than duplicating them —
Phase 1 had already anticipated the unresolved criticality-tier and I13-ledger
gaps this phase runs into.

## Recommendation lifecycle

`LifecycleStatus` is the backend's own state machine — deliberately separate
from the existing `RecommendationStatus` enum, which mirrors the frontend's
six-value vocabulary and has no way to say "awaiting manual SAP execution"
without inventing a new UI-facing meaning. `to_recommendation_status()` is the
one place that projects the richer internal state down to that vocabulary.

```
NOT_EVALUABLE          a mandatory upstream input is missing or blocked
READY_FOR_REVIEW       every mandatory input for this path computed
PENDING_APPROVAL       submitted, awaiting one specific role
APPROVED / REJECTED / SENT_BACK / ADJUSTED
SAP_EXECUTION_PENDING  final approval reached — I07 stops here
SAP_EXECUTED           execution evidence recorded (not proof of adoption)
ADOPTED / PARTIALLY_ADOPTED / NOT_ADOPTED
```

A recommendation is **never** promoted to `READY_FOR_REVIEW` unless its own
path's mandatory inputs actually computed. A blocked calculation stays
`NOT_EVALUABLE`; there is no approval of an incomplete recommendation.

## Normal path

Material-plants Phase 3 classified as `SUFFICIENT`. `READY_FOR_REVIEW` only
when Phase 5's `safety_stock_status` and `rop_status` are both `SUCCESS`.
**Today: 0 of 471** — the service-level matrix is unsigned, so every normal
recommendation is `NOT_EVALUABLE`.

## OAR / cold-start path

Material-plants Phase 3 routed to `NO_HISTORY` or `COLD_START`. Phase 6's
similarity result is consumed as-is — nothing here recomputes similarity.

**Similarity availability and recommendation reviewability are different
facts, and only the second may produce `READY_FOR_REVIEW`.** An earlier
version of this builder granted `READY_FOR_REVIEW` whenever Phase 6 found
neighbours, reasoning that the neighbour list and scores were real evidence
for a human to review regardless of whether Phase 5 could weight them into a
number. That conflated *"there is something to look at"* with *"there is a
value to approve"* — the workflow's own rule is that a recommendation must
carry all mandatory inputs for **its own path**, and for the OAR path that
mandatory input is the weighted estimate, not merely the neighbours behind it.
The rule is now:

```
OAR similarity:          AVAILABLE / NOT_AVAILABLE
OAR neighbours:          oar_neighbour_count, oar_best_similarity, confidence
OAR inventory estimate:  Phase 6's own EstimateStatus, carried through unchanged
Recommendation status:   READY_FOR_REVIEW  only when the estimate is SUCCESS
                          NOT_EVALUABLE     otherwise, with blocking_reason
                                            = "SERVICE_LEVEL_UNSET" when that
                                              is specifically why
```

The similarity result is **never discarded** when the recommendation itself
is blocked: `oar_similarity_status`, `oar_neighbour_count`, `oar_best_similarity`
and `confidence` are populated on every OAR recommendation that had eligible
candidates, whether or not the estimate could be computed. Only the
recommended SS/ROP/Max fields and the overall `status` are gated on the
estimate actually succeeding.

**Today: 741 of 44,938 have `oar_similarity_status = AVAILABLE`** (real
neighbours, real scores) **and all 741 are `NOT_EVALUABLE`** with
`blocking_reason = SERVICE_LEVEL_UNSET`, because zero neighbours have a
successful Phase 5 calculation to lend from (Phase 5's own blocker, inherited
unchanged). The remaining 44,197 have `oar_similarity_status = NOT_AVAILABLE`
(`NO_ELIGIBLE_NEIGHBORS`). **Zero OAR recommendations are `READY_FOR_REVIEW`**
on the current configuration — this is the correct state, not a regression:
no neighbour anywhere in the catalogue currently has a value to lend.

## OAR → Min-Max conversion

Separate from OAR identification, per the Formula Reference:

```
ConversionEligible = (ConsumptionCount12M > 4)
                   OR (CriticalOrSignificantProductionImpact = TRUE)
                   OR (I13_HOD_Approved = TRUE)
```

**What is actually counted for trigger 1**: Phase 3's `non_zero_periods` —
the count of non-zero-demand months over the observed window, the same
population the ADI derivation itself uses. No separate transaction count
exists upstream.

**Trigger 2 (production impact) is unresolved** — the FRS's own two
statements name different criticality tier sets, and Phase 1's
`ConversionTriggerPolicy.criticality_trigger_tiers` is `None` for exactly
that reason. Evaluating it raises `PolicyNotConfiguredError`, caught and
reported as part of the UNKNOWN reason rather than resolved here.

**Trigger 3 (I13 HOD approval)** is read through a `HodApprovalLookup`
interface with one implementation today: `NoHodLedgerAvailable`, since
`app/initiatives/i13/` is an empty stub. Returns `None` — never assumes no
request exists.

A fired trigger wins regardless of what the others report. Only when nothing
fires does an unresolved trigger make the verdict `UNKNOWN` rather than a
confident `NOT_ELIGIBLE`; a *disabled* trigger, by contrast, definitely does
not fire and never contributes to `UNKNOWN`.

**Today: 44,938 of 44,938 OAR recommendations report `UNKNOWN`** — every one
hits the unresolved criticality-tier policy and the missing I13 ledger, and
none has a trigger that fires on its own first.

## Explanation

No LLM. Every `RecommendationFactor` is built from a value the builder
actually received — there is no path from "nothing computed" to a sentence
claiming savings, reduction or improved service. `expected_impact()` computes
conservative current-vs-recommended deltas and reports
`NOT_EVALUABLE_COST_DATA_UNAVAILABLE` for any monetary figure, since no
holding rate exists in any I07 document or seeded table.

### Example — blocked, normal path

```
material 1000000009, LUMPY, SBA baseline, PLANNED_FALLBACK lead time (1 PO)
blocking_reason: the Criticality x Circuit service-level matrix must be
  supplied and signed by Vedanta (Solution Design, Rule 1); recommendations
  are blocked until then
factors:
  Demand pattern: Demand class is LUMPY.
  Forecasting model: SBA was selected as the baseline model.
  Lead time: PLANNED_FALLBACK, 1 PO(s)
  Blocked: Recommendation is blocked because the service-level policy is not configured.
trace: blocked_status=NOT_EVALUABLE_SERVICE_LEVEL_UNSET; reason=<as above>
```

### Example — OAR, similarity found, estimate blocked

```
material 4000002724, 4 eligible candidates, 4 neighbours, best similarity 0.2184
confidence: LOW, conversion: UNKNOWN
trace: candidates_considered=471; eligible_candidates=4; neighbour_count=4;
  best_similarity=0.218425; estimate_status=NOT_EVALUABLE_SERVICE_LEVEL_UNSET
```

## Approval workflow

The existing four-step chain, exactly:

```
End User → Engineering Manager → Commercial Manager → Warehouse Supervisor
    → SAP_EXECUTION_PENDING
```

Implemented as pure functions over an explicit `WorkflowState`
(`workflow.py`), so every rule is checked without a database: the correct
role can act, the wrong one cannot, stages cannot be skipped. `REJECT`,
`SEND_BACK` and `ADJUST` require a non-empty comment; `APPROVE` alone may
proceed silently. `ADJUST` never mutates a value in place — it increments an
adjustment counter and writes a `RecommendationVersion` snapshot, then
re-enters the chain. `SEND_BACK` returns to the immediately preceding role,
not to the start.

**Final `APPROVE` at Warehouse Supervisor produces `SAP_EXECUTION_PENDING`
and stops.** There is no code path from any workflow action to a SAP call —
verified by an AST-based test that no networking module is imported anywhere
in the package.

## Approval ledger

`ApprovalLedgerEntry` — one immutable row per action: recommendation id and
version, actor, role, action, previous/new status, comment, timestamp.
Written by `ledger.py` in the same call that applies the state transition, so
the two can never disagree about what happened. Rows are never updated or
deleted; a recommendation's own `status` moves on, its ledger history does
not — verified against a real database by approving a recommendation through
all four steps and confirming five ledger rows (one SUBMIT + four APPROVE)
survive intact.

## Manual SAP execution boundary

`execution.py` records what a human reports about a manual SAP transaction —
approved value snapshot, executed-by, timestamp, SAP reference, status
(`PENDING` / `EXECUTED` / `FAILED` / `NOT_CONFIRMED`), free-text evidence.
Only a recommendation at `SAP_EXECUTION_PENDING` may receive evidence.
`EXECUTED` moves the recommendation to `SAP_EXECUTED`; every other status
leaves it at `SAP_EXECUTION_PENDING`. **The module has no HTTP client and
imports no networking library** — actual VZI manual execution occurs
entirely outside this system.

## SAP adoption / reconciliation

Read-only, through a `SapStateProvider` interface. **No implementation
exists today** — Phase 2's staging layer never materialised CDHDR/CDPOS, so
`NoSapStateAvailable` is the only provider, and every reconciliation reports
`UNKNOWN`. This is deliberate: querying `raw_cdhdr`/`raw_cdpos` directly from
this package would silently reintroduce the SAP-table coupling the adapter
architecture exists to prevent.

```
Conversion adoption:  MRP Type = VB AND MINBE populated AND MABST populated
Parameter adoption:   observed EISBE/MINBE/MABST == approved values
```

`ADOPTED` (all match) / `PARTIALLY_ADOPTED` (some match) / `NOT_ADOPTED`
(none match, evidence was actually read) / `UNKNOWN` (no evidence exists at
all). **Unchanged is never conflated with proven unchanged** — with no
baseline, the question cannot be answered either way, so `UNKNOWN` is
returned rather than asserting `NOT_ADOPTED`.

## Versioning and idempotency

A recommendation row is identified by `(material, plant, feature_run_id,
forecast_run_id, inventory_run_id, oar_run_id, policy_id, policy_version,
formula_version)` under a unique constraint. Regenerating with unchanged
upstream inputs reuses the existing row; any upstream run or policy version
changing produces a new one. `recommendation_id` (e.g.
`REC-STD-1000000009-1300`) is a stable, human-readable label for "the
recommendation for this material-plant" — deliberately **not** a
database-unique key, since a legitimate regeneration under new run ids must
be able to coexist with the row it supersedes rather than raise a
uniqueness violation.

`formula_version` is bumped (`i07-recommendation-1` → `-2`) whenever the
assembly logic itself changes — as it did for the READY_FOR_REVIEW
correction above — precisely so a logic fix forces regeneration under a new
identity rather than being silently absorbed by the "unchanged inputs, reuse
the row" idempotency path. Rows written under a superseded formula version
represent recommendations produced under logic later found incorrect and are
not retained.

## Policy gates, preserved from earlier phases

Nothing here resolves an unsigned policy. Every one of these still blocks
exactly as Phases 1–6 left it: the service-level matrix, the Max Stock
strategy, the OAR rule confirmation and roll-up, the conversion
criticality-tier set, the I13 HOD ledger, the obsolescence trigger, the
lead-time ownership question. `Max Stock = 2 × ROP` does not exist anywhere
in this package — checked by a dedicated test.

## Current development-data results

45,409 material-plants, all covered exactly once, **all `NOT_EVALUABLE`**:

| Path | Status | Count | Why |
| --- | --- | --- | --- |
| Normal | NOT_EVALUABLE | 471 | service-level matrix unsigned |
| OAR, similarity AVAILABLE | NOT_EVALUABLE | 741 | estimate blocked (`SERVICE_LEVEL_UNSET`) |
| OAR, similarity NOT_AVAILABLE | NOT_EVALUABLE | 44,197 | `NO_ELIGIBLE_NEIGHBORS` |

Conversion eligibility: **44,938 UNKNOWN** (criticality tiers unresolved, no
I13 ledger). SAP adoption: not yet exercised against real evidence — no
provider exists.

## Testing

Workflow, conversion, adoption and explanation are pure-function unit tests
(no database). The generation service, ledger immutability and execution
evidence are checked against the real extract. Boundary tests assert, by AST
inspection rather than substring search (which trips on legitimate prose in
docstrings): no raw SAP table reference, no `EXTWG`, no hardcoded service
level, no `2 × ROP`, no networking import in `execution.py` or `workflow.py`.

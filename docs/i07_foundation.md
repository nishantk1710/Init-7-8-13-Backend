# I07 Foundation (Phase 1)

What exists after Phase 1, and the boundaries it establishes. Business rules
themselves live in the Solution Design and Formula Reference — this describes
where they are *enforced*, not what they say.

```
app/initiatives/i7/
├── contracts/     canonical, source-independent domain types
├── policy/        versioned business rules + validation
└── errors.py      domain error hierarchy

app/models/i7_policy.py     stored policy versions
tests/i7/                   132 tests
```

No forecasting, no calculations, no ingestion, no API routes. Phase 1 is the
skeleton those hang off.

## The canonical contract boundary

```
July extract ──┐
live OData ────┼──→ adapter ──→ canonical contracts ──→ I07 domain
RFC/BAPI ──────┘
```

Everything above the contract line — features, classification, forecasting,
calculation — speaks only `app.initiatives.i7.contracts`. Nothing there imports
`app.integrations.sap`, reads a `raw_*` table, or imports SQLAlchemy. Swapping
the data source is then an adapter change (Phase 2 for the extract, Phase 12 for
live OData), not a rewrite of the engines.

`tests/i7/test_boundaries.py` parses the source tree and fails if that is
violated, because an errant import passes every behavioural test while quietly
welding the domain to one source.

**Unavailable is `None`, never `0`.** The frontend's SAP mapper currently emits
`serviceLevelTarget: 0` and `unitPrice: 0` for values it cannot source; the UI
then renders a 0% service level as a target, and `Φ⁻¹(0)` is negative infinity. A
zero meaning "unknown" is indistinguishable from a zero meaning zero, and only
one is safe to calculate with.

## The policy boundary

Business rules are **not** in `app/core/config.py`. That holds infrastructure —
connection strings, credentials, timeouts — which varies per environment and is
set by whoever deploys. Policy holds business rules, which are identical in every
environment and are set by Vedanta. They also version differently: nobody asks
what the database URL was in March, but a recommendation must be explainable
against the rules in force when it was produced.

Two states are kept distinct throughout:

| State | Meaning | Behaviour |
| --- | --- | --- |
| configured | present, valid, usable | calculation proceeds |
| unconfigured | no business decision yet | `PolicyNotConfiguredError` |

This implements Solution Design Rule 1 — *"If policy is unsigned → system blocks
recommendations"*. A guessed service level does not fail loudly; it produces a
plausible, wrong, approved safety stock.

## Policy versioning

`PolicyDocument` is immutable and identified by `(policy_id, policy_version)`.
Every `Recommendation` carries that pair as a mandatory field, so the exact rules
behind any number can be reconstructed months later.

Stored in `i7_policy_version` as serialised JSON in a `Text` column — not
`JSONB`, because the deployed database may not be Postgres and I07 never queries
*inside* a policy, it loads one whole. A policy change means a new row at a
higher version, never an update in place.

`status` is `DRAFT` until Vedanta signs. Only `SIGNED` may produce
recommendations, and `require_ready_for_recommendations()` enforces both that and
the absence of unresolved policies.

## Unresolved policies

`PolicyDocument.unresolved_policies()` returns what Vedanta is still blocked on.
Today, all six:

| Policy | Why unset |
| --- | --- |
| `service_level_matrix` | Rule 1: *"Do NOT invent percentages."* No values supplied, and **no `circuit` field exists in any of the 27 seeded tables** though the matrix is keyed on it |
| `max_stock_strategy` | Rule 2: formula needs sign-off. EOQ needs ordering cost + holding rate, which appear in no document and no table; review-period needs `T` per tier, also unsupplied |
| `adoption_monitoring_window` | FRS calls it configurable, never states a value |
| `conversion_criticality_tiers` | FR-5 says "Critical"; §3.1 says "Critical **or significant production impact**" — different tier sets |
| `oar_rule_confirmation` | See below |
| `oar_rollup_policy` | Per-plant vs material-level is a team-lead call |

Values the documents **do** specify are preserved as defaults: ADI 1.32, CV² 0.49,
history gate 5/6, adoption >5% pinball and ≤5% bias over ≥12 origins, similarity
0.35/0.30/0.35 with K 5–10, confidence 24/12 months.

Confidence thresholds are kept exactly as documented even though the July/August
extract reaches at most 12 months and nothing can grade HIGH. That is a finding
about the data; relaxing the threshold would change what HIGH means to an
approver.

## OAR identification

Three documents disagree:

| Source | Rule |
| --- | --- |
| Solution Design / Formula Reference | `MRP_TYPE ∈ {PD, ND}` |
| FRS v1.3 | `MARA.EXTWG = 100` |
| Checked-in project rule | `DISMM ∈ {ND, PD} AND MSTAE ≠ '01'`, per material-plant |

The third is implemented: it is in force, it is the only one with a live scan
behind it, and it is a **conjunction over two fields** — a policy shaped as "pick
one field, list its values" cannot express it.

So `OarPolicy` is a list of predicates combined by `AND`. The current rule is
stated exactly; a later ruling is a configuration edit. Three properties matter:

- **`EXTWG` is retired.** It stays on `MaterialAttributes` for source fidelity
  and audit, but `PredicateField` does not offer it, so no rule can name it. A
  leakage test mirrors the frontend's `no-leakage.test.ts`.
- **Evaluation is three-state, in general.** The machinery supports
  `IN_SCOPE`/`OUT_OF_SCOPE`/`UNKNOWN` for any predicate, and definite
  exclusion still beats unknown, which still beats inclusion. For the
  current OAR rule specifically, a blank `DISMM` no longer resolves to
  `UNKNOWN` -- it is business-confirmed as `IN_SCOPE` (`blank_means_in_scope
  =True`), reversing the original design (47% of live rows have no `DISMM`
  value, which is why this mattered enough to design for explicitly).
- **The field rule (including blank) is confirmed; roll-up is not.**
  `OarPolicy.confirmed` still defaults to `False` on the backend object --
  that flag gates the policy as a whole, including the still-undecided
  `rollup`, not the field rule in isolation (the frontend's parallel
  `ScopeDefinition.confirmed` is `true`, since its scope has no roll-up
  field to also gate). The live scan found ND+PD = 46.4% against a plan
  wanting <40% -- with blank now also in-scope, OAR coverage is roughly
  double that -- plus six undocumented MRP codes (`V1`, `M0`, `RP`, `VI`,
  `VH`, `V2`), which remain `OUT_OF_SCOPE`.

## Identity mapping boundary

The platform and SAP do not share identities, and no exposed SAP field supplies
the mapping:

```
material   app "500-14892"    SAP "000000000080000000"
plant      app "PLANT-GBG"    SAP "3000"
```

`MaterialIdentity` and `PlantIdentity` carry both side by side. The SAP value is
required (it is where data comes from); the app value is optional and **never
derived** — absent stays absent until VZI supplies a mapping table, which then
populates a field that already exists.

This matters because every cross-initiative lookup in the frontend searches by
app-side identity. An API returning a bare SAP number breaks Material 360 with no
error — it simply finds nothing.

`MaterialPlantKey` is the grain almost everything is evaluated at: `DISMM` lives
on MARC, so a material can be OAR at one plant and planned at another.

## Errors

```
I07Error
├── ConfigurationError        a value is present but wrong
├── PolicyNotConfiguredError  no business decision yet
├── ContractError             source data cannot form a valid record
└── ValidationError           a domain invariant was violated
```

The first two are separate on purpose. A negative ADI cutoff is a typo someone
fixes now; an unsigned service-level matrix is a blocked sign-off. Reporting both
as "bad config" sends an engineer hunting a bug that is really a business
decision.

## What Phase 2 inherits

- Adapters map the extract onto these contracts. The business-label →
  canonical-field mapping (`mrp_type` → `DISMM`) is Phase 2's job.
- `ConsumptionSeries` requires a **dense** monthly series: zero-demand months
  must be present explicitly. `ADI = n / n_nz` counts all periods, so a dropped
  empty month shrinks `n` and misclassifies intermittent demand as smooth. The
  contract rejects gaps rather than tolerating them.
- Nothing writes to SAP, in any phase. P1 forbids write-back programme-wide.

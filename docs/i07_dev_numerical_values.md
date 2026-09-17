# I07 — Numerical SS/ROP/Max in DEV/TEST

How the OAR similarity engine (Phase 6) is made to produce actual numerical
Safety Stock, ROP and Max Stock for development and testing, and why it
produced none before.

Nothing in this document describes a business decision. Two Vedanta policies
remain unsigned, and both still block production exactly as they did before —
see [Production safety](#production-safety) for the guarantees, and
[When Vedanta signs](#when-vedanta-signs) for what to delete.

```
Phase 5 donor calculation            Phase 6 OAR target
  service level -> Z                   similarity -> >=5 neighbours >=0.60
  -> safety stock                        -> borrow donor SS/ROP/Max
  -> ROP                                 -> weighted average
  -> Max Stock                           -> recommendation
        |                                        ^
        +------- donors must ALL THREE succeed --+
```

## The problem

OAR borrows its parameters from neighbours; it computes none of its own. So a
cold-start target produces numbers only when enough neighbours have their own
successful Phase 5 calculation. Every target was instead reporting
`NOT_EVALUABLE_SERVICE_LEVEL_UNSET` or `NOT_EVALUABLE_NEIGHBOR_INVENTORY`.

## Root cause

Two independent blockers. Each is sufficient on its own, which is why fixing
only the service level appeared to change nothing.

### Cause A — an unconfigured Max Stock strategy blocked every estimate

`oar/repository.load_inventory_values` admits a donor only when **all three**
of its Phase 5 statuses are SUCCESS:

```python
if (row.safety_stock_status == "SUCCESS"
    and row.rop_status == "SUCCESS"
    and row.max_stock_status == "SUCCESS"):
```

`PolicyDocument().max_stock` has `strategy=None`, so `strategy_for()` returns
`NotConfiguredMaxStockStrategy` and **every** Phase 5 row carries
`max_stock_status=NOT_CONFIGURED`. The map comes back empty, no neighbour is
`inventory_eligible`, and the estimate blocks — however good the similarity
scores and however many neighbours qualified.

This is the non-obvious one: safety stock and ROP could both be succeeding and
the estimate would still produce nothing.

### Cause B — the DEV flag never reached the OAR run

`run_forecasting` and `run_inventory_calculations` both defaulted to
`dev_fixtures.default_policy()`. `run_oar_similarity` and
`generate_recommendations` defaulted to a bare `PolicyDocument()`.

So with `I7_DEV_MOCK_SERVICE_LEVEL=true`, Phase 5 wrote real donor values into
`i7_inventory_calculation` while Phase 6 read an unconfigured matrix off a
policy the flag had never reached. `_oar_service_level_configured` returned
`False` and every target reported `NOT_EVALUABLE_SERVICE_LEVEL_UNSET` with its
neighbours' numbers sitting in the database.

### Isolated, one cause at a time

Six synthetic SMOOTH donors, one cold-start target, same inputs throughout:

| Scenario | donor SS | donor ROP | donor Max | borrowable | OAR estimate |
|---|---|---|---|---|---|
| **A** neither fixture (production) | — | — | `NOT_CONFIGURED` | 0/6 | `NOT_EVALUABLE_SERVICE_LEVEL_UNSET` |
| **B** service level only | 5 | 15 | `NOT_CONFIGURED` | **0/6** | `NOT_EVALUABLE_NEIGHBOR_INVENTORY` |
| **C** max stock only | — | — | — | 0/6 | `NOT_EVALUABLE_SERVICE_LEVEL_UNSET` |
| **D** both fixtures | 5 | 15 | 25 | **6/6** | **SUCCESS — SS 6, ROP 16, Max 27** |

Row B is Cause A in isolation: donor SS and ROP both succeed and the estimate
still yields nothing.

## What changed

| File | Change |
|---|---|
| `config/dev/i7_max_stock_strategy.yaml` | **new** — DEV fixture selecting the existing `review_period` strategy |
| `app/initiatives/i7/policy/dev_fixtures.py` | added `load_mock_max_stock_policy()`; `default_policy()` assembles both mocks independently |
| `app/core/config.py` | added `i7_dev_mock_max_stock: bool = False` |
| `app/initiatives/i7/oar/service.py` | `run_oar_similarity` defaults to `default_policy()` |
| `app/initiatives/i7/recommendations/service.py` | `generate_recommendations` defaults to `default_policy()` |
| `.env.example` | documented `I7_DEV_MOCK_MAX_STOCK=false` |

No formula was touched. `inventory/max_stock.py`, `inventory/safety_stock.py`,
`inventory/rop.py`, `oar/estimate.py`, `oar/ranking.py` and the
`MaterialFeature` model are unmodified.

## Running it

```bash
I7_DEV_MOCK_SERVICE_LEVEL=true
I7_DEV_MOCK_MAX_STOCK=true
```

Both are required. The Max Stock flag alone changes nothing, because Max Stock
is computed from ROP and ROP is blocked by an unsigned service level.

To run the whole chain and review the result:

```bash
python -m scripts.run_full_pipeline              # run every phase, then report
python -m scripts.run_full_pipeline --report-only # report the latest runs only
python -m scripts.run_full_pipeline --blocked-only --rows 0
```

`scripts/run_full_pipeline.py` calls the same phase services and reports what
they wrote as terminal tables: the Phase 4 backtest metrics and model
parameters behind each champion, the Phase 5 SS/ROP/Max per material with
their statuses, the OAR targets and the neighbours they borrowed from, and a
coverage summary answering how many material-plants ended up with all three
numbers and what blocks the rest. It computes nothing itself.

### DEV service level

The **existing** fixture mechanism, unchanged — deliberately no second
service-level configuration system. `config/dev/i7_service_level_matrix.yaml`
supplies `NORMAL: 0.85`, and Z stays `scipy.stats.norm.ppf(0.85) = 1.036433`,
derived downstream by `inventory/service_level.z_factor` and never hardcoded.

### DEV Max Stock

Strategy `review_period` — the already-implemented `Max = ROP + (D_rate x T)`,
with `T = 1.0 month` for CRITICAL, IMPACT, INSURANCE and NORMAL. OBSOLETE is
absent because `inventory/service.py` short-circuits it to
`NOT_APPLICABLE_OBSOLETE` before Max Stock is reached.

The review period lives in YAML, labelled `DEV/TEST ONLY — NOT VZI PRODUCTION
POLICY`, never in business logic. EOQ is deliberately not mocked: it needs an
ordering cost *and* an annual holding rate, neither of which appears in any
I07 document or seeded table, so mocking it would invent two business figures
with no source to replace them from.

## Criticality is not touched

`MaterialFeature.criticality` has no default, no server default, and remains
nullable. Phase 5 still resolves **each material's own real criticality** via
`_criticality(row.criticality)` — a CRITICAL donor resolves at 0.98, not at
the OAR gate's temporary NORMAL. The fixtures configure the *matrix*, never
the *key* it is looked up by.

A donor with NULL criticality still blocks rather than defaulting: criticality
is the matrix key, and there is nothing to look up without it.

The OAR temporary NORMAL (`OAR_TEMPORARY_CRITICALITY`) is not merely a gate
bypass. The OAR gate and the Phase 5 donor calculations now resolve against
the **same** dev matrix, reached through the same `default_policy()`.

## Worked example

Six donors, one target, text similarity unavailable (weights renormalised over
structured and business):

| neighbour | similarity | SS | ROP | Max |
|---|---|---|---|---|
| D000001 | 0.9875 | 5 | 15 | 25 |
| D000006 | 0.9875 | 4 | 13 | 22 |
| D000005 | 0.9625 | 5 | 16 | 27 |
| D000002 | 0.9500 | 6 | 18 | 30 |
| D000003 | 0.9250 | 4 | 12 | 20 |
| D000004 | 0.8875 | 7 | 22 | 37 |

```
SS_oar  = sum(s_i x SS_i)  / sum(s_i) -> 6
ROP_oar = sum(s_i x ROP_i) / sum(s_i) -> 16
Max_oar = sum(s_i x Max_i) / sum(s_i) -> 27
```

Rounded up once, at the end. Label: `SIMILARITY-BASED ESTIMATE`.

## Gates are unchanged

| Gate | Value | Behaviour |
|---|---|---|
| `minimum_similarity` | 0.60 | hard admission floor, applied with `>=` before the Top-K cut |
| `minimum_neighbours` | 5 | 1–4 qualifying neighbours produce **no** partial estimate |
| `maximum_neighbours` | 10 | Top-K cap |
| `minimum_neighbour_history_months` | 12 | hard eligibility constraint |

A candidate scoring 0.5999 with a perfectly good Phase 5 calculation
contributes nothing. Four neighbours at 0.98 produce nothing. Both are pinned
by test.

## API

Unchanged and already correct — no duplicate fields were added. When
`estimate_status == "SUCCESS"`, `build_oar_recommendation` carries
`t.safety_stock / t.rop / t.max_stock` into `recommended_safety_stock / _rop /
_max_stock`, sets `READY_FOR_REVIEW` and
`safety_stock_method="similarity_weighted"`:

```
i7_oar_target -> repository -> BuiltRecommendation -> Recommendation
              -> RecommendationDetail -> StockParameters.recommended -> FastAPI
```

## Tests

60 tests across two files.

`tests/i7/test_dev_max_stock_fixture.py` (24) — containment: the fixture is
never `PolicyDocument()`'s default, never alone signs a policy, introduces no
new algorithm (the strategy name must resolve to one that already existed),
fails loudly on a typo, and cannot silently become production configuration.
The review period is proved to come from the file in both directions — no
`1.0` literal in the strategy or the orchestration, and a fixture written with
`T = 3.0` changes the computed Max from 25 to 45. Also asserts no `2 x ROP`
fallback was introduced, which Solution Design Rule 2 prohibits by name.

`tests/i7/test_oar_dev_numerical_values.py` (36) — the whole chain: the DEV
service level reaching the donor calculation, Z still from `norm.ppf`,
criticality untouched, the 5-neighbour gate refusing a partial estimate from
four good neighbours, a sub-0.60 candidate carrying SS/ROP/Max of 9999 proven
not to leak into the average, each weighted average checked against hand
arithmetic rather than against itself, and the production default still
blocking every leg.

Three groups are worth naming because they pin the fix rather than the
behaviour around it:

* **Policy wiring** — all four `run_*` entry points resolve through
  `default_policy()`; `run_oar_similarity` and `generate_recommendations` are
  each observed *calling* it (the run is stopped at its first database access,
  so no connection is needed); and an explicit policy argument still wins.
* **The verified scenario** — the six-donor table and `SS = 6, ROP = 16,
  Max = 27` are pinned as literals, so a change in any upstream formula
  surfaces as a specific number rather than a still-passing relative
  assertion.
* **Isolation** — no calculation module imports a fixture, and the four
  orchestrators may call `default_policy()` but never a `load_mock_*` loader,
  so there is exactly one place a fixture can enter a run.

Regression check: `tests/i7` went from 12 failed / 713 passed / 216 skipped to
12 failed / **773** passed / 216 skipped — the same 12 pre-existing
DB-dependent failures, plus the 60 new tests.

## Production safety

Five independent guarantees, any one of which alone keeps the fixtures out of
production:

1. Both flags default to `false` in every environment, including production.
2. With neither set, `default_policy()` returns exactly the `PolicyDocument()`
   the two changed call sites previously constructed — equivalent behaviour.
3. `inventory/max_stock.py` holds no reference to `dev_fixtures`, the loader,
   or the YAML, so nothing in the calculation path can reach the fixture.
4. A loaded mock leaves `status=DRAFT`; nothing in the codebase ever sets
   `SIGNED`, so `require_ready_for_recommendations()` still blocks.
5. Production still blocks at every leg — scenario A above.

One caveat worth knowing: a loaded mock **does** remove its entry from
`PolicyDocument.unresolved_policies()`, because that method reports the shape
of the document and a mock fills the field in. `status` is what keeps the two
facts apart — a mock can never, on its own, make a document claim to be a
signed Vedanta decision.

## Real-data verification

**BLOCKED.** `DATABASE_URL` is set but names the `postgresql+psycopg` backend,
which `app/core/db.py` refuses by design (this system runs against Azure SQL
only; local Postgres was a stand-in and has been removed). The `psycopg`
driver is not installed either, and the Azure SQL server is unreachable
outside the VNet.

No population figures — successful Phase 5 donors, OAR targets with >=5
qualifying neighbours, targets producing numbers — could therefore be
measured, and none are stated. They need an `mssql+pyodbc://...` URL reachable
from the machine running the pipeline.

## Frontend

The recommendation workbench (repo `Init-7-8-13-Frontend`) already displays
Recommended Safety Stock, ROP and Max in `parameter-comparison.tsx`,
`recommendations-table.tsx` and `approvals-workspace.tsx`.

It renders them from static scenario data
(`src/features/initiative-7/data/recommendations.ts`), not from the backend —
the API client is used only for `/api/health`. `StockParameters` is typed
`{rop: number; safetyStock: number; maxStock: number}`, non-nullable, so a
blocked value has no representation.

Wiring the workbench to the backend means new API integration for the list and
detail views, nullable types, and loading/error states. That is a deliberate
piece of work rather than a minimum display fix, and it has not been done.

## When Vedanta signs

Outstanding business dependencies, and what to remove for each:

| Dependency | Delete |
|---|---|
| Criticality x Service Level matrix (Rule 1) | `config/dev/i7_service_level_matrix.yaml`, `load_mock_service_level_policy()`, `I7_DEV_MOCK_SERVICE_LEVEL` |
| Max Stock formula and review periods (Rule 2) | `config/dev/i7_max_stock_strategy.yaml`, `load_mock_max_stock_policy()`, `I7_DEV_MOCK_MAX_STOCK` |
| A real OAR service-level rule | `OAR_TEMPORARY_CRITICALITY` and `_oar_service_level_configured` in `oar/service.py` |

Replace each YAML file's *values*, never its shape, and the loaders keep
working until the flags themselves are removed.

A fourth item is a data question rather than a policy one: donors with NULL
criticality remain blocked by design, since criticality is the matrix key. No
DEV fallback was added, because the architecture intentionally keeps real
criticality for real materials. If real-data coverage proves thin, that is the
next decision to take — and it is a business decision, not a code one.

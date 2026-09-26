# I07 API (Phase 8)

Exposes the Phase 1–7 I07 domain services through a versioned, read-mostly
FastAPI layer. The API is an access layer, never a second business engine:

```
Frontend / API client
       ↓
FastAPI route (thin)
       ↓
I07 application/service layer  (Phase 4/5/6/7, unchanged)
       ↓
Database
```

No route recalculates ADI, CV², a forecast, a safety-stock formula, ROP, Max
Stock, OAR similarity, a workflow transition or an adoption classification.
Every one of those already exists in `app.initiatives.i7.*` from Phases 1–7;
the API reads their persisted output or calls their existing functions
directly.

## Domain mapping

| API endpoint | Existing service | Existing input | Existing output |
| --- | --- | --- | --- |
| `GET /recommendations` | `i7_recommendation` table (Phase 7) | SQL filter/sort/paginate | `Recommendation` rows |
| `GET /recommendations/{id}` | same | recommendation_id | one `Recommendation` row |
| `GET /recommendations/{id}/trace` | same, `calculation_trace`/`factors_text` columns | recommendation_id | Phase 7's stored explanation |
| `POST /recommendations/{id}/submit` | `recommendations.ledger.submit_for_approval` | `Recommendation`, actor_id | new `WorkflowState` + ledger row |
| `POST /recommendations/{id}/actions` | `recommendations.ledger.apply_approval_action` (→ `workflow.apply_action`) | `Recommendation`, actor, role, action, comment | new `WorkflowState` + ledger row |
| `GET /recommendations/{id}/approval-history` | `i7_approval_ledger` table | recommendation_id | ordered `ApprovalLedgerEntry` rows |
| `GET /recommendations/{id}/adoption` | `recommendations.adoption.evaluate_conversion_adoption` / `evaluate_parameter_adoption` | material, plant, approved values | `AdoptionResult` |
| `GET /runs` | `i7_feature_run` / `i7_forecast_run` / `i7_inventory_run` / `i7_oar_run` | — | recent run rows |
| `GET /runs/{run_type}/{run_id}` | same | run_type, run_id | one run row |

## Base URL

```
/api/v1/i7
```

`/api` is the application's existing global prefix (`Settings.api_prefix`,
unchanged); `/v1/i7` is this phase's addition, mounted once in
`app.api.router`.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | I07 liveness |
| GET | `/recommendations` | Paginated, filtered, sorted list |
| GET | `/recommendations/{id}` | Full detail |
| GET | `/recommendations/{id}/trace` | Calculation/explanation trace |
| POST | `/recommendations/{id}/submit` | Enter the approval chain |
| POST | `/recommendations/{id}/actions` | APPROVE / REJECT / SEND_BACK / ADJUST |
| GET | `/recommendations/{id}/approval-history` | Immutable ledger |
| GET | `/recommendations/{id}/adoption` | Read-only SAP reconciliation |
| GET | `/runs` | Recent pipeline runs, all four types |
| GET | `/runs/{run_type}/{run_id}` | One run's detail |

## Request/response schemas

Defined under `app/schemas/i7/` (`recommendations.py`, `approvals.py`,
`adoption.py`, `runs.py`, `health.py`, `errors.py`) — separate Pydantic models
from the SQLAlchemy rows in `app/models/i7_recommendation.py`. No route
returns an ORM instance.

`recommendation_id` in a path is a **stable label, not a database-unique key**
(Phase 7: a pipeline regeneration under new upstream run ids legitimately
produces a second row for the same material-plant). Every GET/POST resolves to
the most recently generated row for that id.

## Status codes

200 (successful GET/action) · 400 (invalid sort field) · 404 (recommendation
or run not found) · 409 (workflow action invalid for the current role/stage,
or a required comment is missing) · 422 (Pydantic/FastAPI validation — bad
enum value, page_size out of range) · 500 (unexpected, logged, no stack trace
returned).

201 is not used: no endpoint creates a new *addressable* API resource — a
workflow action changes existing recommendation/ledger state.

## Error format

```json
{ "error": { "code": "RECOMMENDATION_NOT_FOUND", "message": "Recommendation was not found.", "details": { "recommendation_id": "..." } } }
```

One handler (`ApiError` → `app.main.api_error_handler`) renders every domain
exception raised from `app/schemas/i7/errors.py`'s `not_found` / `conflict` /
`bad_request` / `forbidden` helpers into this shape. No route formats an error
body itself, and no SQLAlchemy/Postgres exception text reaches a client — the
existing top-level `Exception` handler in `app.main` still catches anything
unmapped and returns a bare 500.

## Pagination

`GET /recommendations` — database `LIMIT`/`OFFSET`, never a Python-side slice
of a fully-loaded table. `page_size` is capped at 200. Ordering is always the
requested sort column **plus the row's own id** as a tiebreaker, so two
requests for the same page return the same rows even when the sort column has
duplicate values — verified by a test that fetches page 1 twice and confirms
identical ordering, then fetches page 2 and confirms no overlap.

## Filtering

Only real, persisted, indexed columns are exposed: `status`, `plant`
(`sap_plant_code`), `material` (`sap_material_number`), `demand_class`,
`is_oar`, `confidence`, `criticality`. All applied in SQL via SQLAlchemy
`.where()`. Nothing is filtered by loading rows into Python.

## Sorting

A whitelist maps an API sort name to a real ORM column
(`app/api/i7/recommendations.py:SORT_FIELDS`) — `generated_at`, `status`,
`material`, `plant`, `demand_class`, `confidence`. An unlisted value never
reaches SQL; it returns `400 INVALID_SORT_FIELD` before a query is built.

## Approval workflow

Uses `app.initiatives.i7.recommendations.workflow`/`ledger` exactly as
Phase 7 built them:

```
End User → Engineering Manager → Commercial Manager → Warehouse Supervisor
    → SAP_EXECUTION_PENDING
```

`POST .../submit` requires READY_FOR_REVIEW. `POST .../actions` takes an
explicit `action` enum (`APPROVE`/`REJECT`/`SEND_BACK`/`ADJUST`) rather than
five separate near-identical endpoints, since the validation and the ledger
write are identical across all four and the phase brief asks not to duplicate
that logic. Every rule — correct role, no stage skipping, non-empty comment
for REJECT/SEND_BACK/ADJUST, immutable ledger entry — is enforced by the
existing `workflow.apply_action`/`ledger.apply_approval_action`, not
re-implemented in the route. A rule violation raises `WorkflowError`, mapped
here to `409 INVALID_WORKFLOW_ACTION`.

**Final APPROVE never calls SAP.** It produces `SAP_EXECUTION_PENDING` and
stops — verified structurally (see "SAP safety boundary" below) and by an API
test that drives a recommendation through all four roles and asserts the
final status.

## Adoption

`GET /recommendations/{id}/adoption` calls the existing Phase 7 evaluators
directly, computed per request rather than read from `i7_sap_adoption`
because **nothing in Phase 7 writes to that table yet** — the evaluators are
pure and cheap, so recomputing on read is simpler than adding a write path
this phase does not otherwise need, and it stays strictly read-only (the
`SapStateProvider` interface has exactly one implementation,
`NoSapStateAvailable`, so no SAP call is ever made). Possible values are
exactly Phase 7's `AdoptionStatus`: `ADOPTED`, `PARTIALLY_ADOPTED`,
`NOT_ADOPTED`, `UNKNOWN`. **UNKNOWN is never turned into NOT_ADOPTED** — with
no staged CDHDR/CDPOS, every result on the current extract is UNKNOWN, which
is the honest answer, not a defect.

## Authentication

**`app/core/security.py` is an unimplemented placeholder** — inspected before
writing this phase, and nothing was invented in its place. Two consequences,
stated plainly rather than papered over:

1. Approval endpoints (`submit`, `actions`) currently accept `actor_id` and
   `actor_role` as **client-supplied request body fields**, not derived from
   an authenticated identity. There is no identity system yet to derive them
   from. A client can currently assert any role.
2. This is a genuine Phase 8 limitation, not a Phase 8 defect: the brief is
   explicit that an authentication system must not be invented, and the
   architecture is intentionally ready for it — the moment
   `app.core.security` exposes a `require_user` dependency, the two request
   fields become derivable from that dependency instead of the request body,
   and the workflow/ledger functions themselves do not change at all.

CORS uses the existing `Settings.cors_origins` (from `FRONTEND_ORIGIN`),
unchanged — no new origin was hardcoded, and `allow_origins=["*"]` was never
introduced.

## SAP safety boundary

**Zero SAP write-back.** No module under `app/api/i7/` or `app/schemas/i7/`
imports `requests`, `httpx`, or `app.integrations.sap` — verified by an
AST-based structural test (`test_i7_api_package_imports_no_sap_write_client`)
rather than by convention. The adoption endpoint reads through
`SapStateProvider`, whose only implementation returns `None` (no evidence);
the execution-evidence recorder from Phase 7 (`execution.py`) is not exposed
by this phase at all — recording manual execution evidence was not in the
required endpoint list and adding it would have been scope creep beyond the
brief.

## Frontend contract comparison

The frontend's `Recommendation` TypeScript type
(`src/features/initiative-7/types/inventory.ts`) and this API's
`RecommendationDetail` describe the same underlying concept from different
ends of a gap that predates this phase (see the Phase 0 `FIELD_SOURCE_GAPS`
finding). Comparing them, without modifying either side:

**Conceptually matching, structurally different:**

| Frontend | Backend | Difference |
| --- | --- | --- |
| `id` | `recommendation_id` | name |
| `material.materialCode` (nested) | `material` (flat) | frontend nests under a `MaterialReference` object; backend is flat |
| `plantId` | `plant` | name |
| `current: StockParameters {rop, safetyStock, maxStock}` | `current: {rop, safety_stock, max_stock}` | field naming (`safetyStock` vs `safety_stock`) |
| `recommended: StockParameters` | `recommended: {...}` | same naming difference |
| `status: RecommendationStatus` (6-value UI enum) | `status: LifecycleStatus` (12-value backend enum, see below) | different vocabularies — deliberately: see below |
| `factors: RecommendationFactor[]` (`{label, detail}[]`) | `trace.factors: string[]` (`"label: detail"` rendered as one line) | backend stores rendered text, not a structured array, per the no-JSONB rule |

**In the frontend type, with no backend equivalent (frontend-only, some
tracked in `FIELD_SOURCE_GAPS.md` as unsourced even from SAP):**

`circuit`, `demandPattern` (a UI-level taxonomy distinct from `demand_class`),
`risk`, `leadTimeDays`/`leadTimeVarianceDays` (numeric; backend has only
`lead_time.method`, the tier name, not the day count — that number lives in
Phase 5's `i7_inventory_calculation` table, not on the recommendation row),
`serviceLevelTarget`, `unitPrice`, `annualConsumption`,
`workingCapitalImpact`, `consumptionHistory`, `championChallenger` (full
champion/challenger comparison object with accuracy percentages — backend has
only a flat `baseline_model` name), `workflow` (frontend models the whole
step-by-step chain inline; backend exposes the equivalent through the separate
`approval-history` endpoint instead), `oarColdStart` (frontend's structured
`{similarMaterials, suggestedRop, suggestedSafetyStock, confidence, factors,
note}` — backend's `oar` group covers the same ground with different field
names and no free-text `similarMaterials` list), `scenarioNote` (frontend
demo-only).

**Backend-only (no frontend equivalent):** every governance/provenance field
(`policy_id`, `policy_version`, `formula_version`, the four `*_run_id`
fields), `history_status`, `oar_similarity_status`/`oar_estimate_status` (the
Phase 7 correction's distinction — the frontend type predates it),
`conversion_eligibility`/`conversion_trigger`/`conversion_detail`,
`blocking_reason`, `impact_status` and the three `*_delta` fields,
`chain_index`/`adjustment_count`/`current_version`, `updated_at`.

**No field was invented on either side to close this gap.** The frontend
mock's `circuit`, `serviceLevelTarget`, `championChallenger` and
`oarColdStart` values are demo fixtures, confirmed by the frontend type file's
own comment on `Circuit`'s `"Unassigned"` sentinel: *"No source produces
circuit for a real material... it is a missing value, not a category."* This
API does not manufacture a circuit, a service level, or challenger-model
accuracy figures to satisfy that shape. Reconciling the two contracts —
renaming fields, deciding whether `demand_pattern` and `demand_class` should
converge, whether `workflow` should be embedded or fetched separately — is
frontend integration work, explicitly out of scope for this phase.

## Known limitations

Inherited, none introduced or resolved by this phase:

- **Service-level matrix unsigned** — every recommendation is currently
  `NOT_EVALUABLE`; the API surfaces this state faithfully rather than hiding
  it.
- **Max Stock strategy unsigned** — `recommended.max_stock` is `null`
  everywhere.
- **Criticality coverage ~1.7%** of material-plants — most `criticality.value`
  responses are `null`, never defaulted.
- **SAP adoption evidence unavailable** on the current extract — every
  `/adoption` call returns `UNKNOWN`.
- **OAR roll-up unresolved** — unchanged from Phase 3/6, not exposed as a
  filter since there is nothing signed to filter by.
- **~13-month development history** — unchanged upstream constraint;
  irrelevant to the API layer itself.
- **No authentication** — see "Authentication" above.
- **`i7_sap_adoption` table is currently unused** — adoption is computed on
  read, not persisted; a future phase may choose to persist it, which would
  be an additive schema change, not a breaking one.

## Local development / testing

```bash
uvicorn app.main:app --reload --port 8000
```

Then:

- `http://localhost:8000/docs` — interactive OpenAPI UI
- `http://localhost:8000/openapi.json` — raw spec
- `http://localhost:8000/api/v1/i7/health` — liveness

```bash
.venv\Scripts\python.exe -m pytest tests/i7/api -q   # API tests only
.venv\Scripts\python.exe -m pytest -q                # full regression suite
```

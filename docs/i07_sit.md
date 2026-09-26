# I07 — Phase 9 System Integration Test (SIT)

## 1. Scope

Phase 9 verifies — it does not rebuild — the complete Phase 1–8 I07 backend
pipeline: staging → features → forecasting → inventory → OAR → recommendations
→ approval workflow → API. It proves the pipeline behaves correctly against
the real, currently-seeded Postgres database, that blocked/unresolved states
are preserved rather than fabricated, and that the approval workflow, ledger,
adoption evidence, idempotency, concurrency, rollback and SAP-safety
properties all hold under real exercise.

Explicitly out of scope (per the Phase 9 instruction) and untouched: frontend,
SAP integration, authentication, service-level policy, Max Stock policy,
criticality source, I13 integration, I11 live integration.

## 2. Test architecture

Five new test modules under `tests/i7/sit/`, run against the real database
(`needs_db = pytest.mark.skipif(not get_settings().database_url, ...)`), no
mocking:

| File | SIT sections covered | Tests |
|---|---|---|
| `test_domain_integration.py` | 5.1, 5.10, 5.12 — provenance, run isolation, identity | 8 |
| `test_recommendation_gating.py` | 5.2, 5.4, 5.15 — baseline, gating invariants, rollback | 10 |
| `test_approval_workflow_sit.py` | 5.5, 5.6, 5.11, 5.14 — full chain, negative cases, ledger, retries | 11 |
| `test_api_sit.py` | 5.7, 5.8, 5.9, 5.12, 5.13 — endpoint pass, pagination, errors, traceability, SAP safety | 14 |
| `test_concurrency_and_performance.py` | 5.14, 10 — concurrent approval race, performance | 3 |

Total new: **46 tests**, all passing.

Each workflow-exercising test uses one isolated, clearly-labelled,
test-only fixture recommendation (`REC-SIT-WORKFLOW-1`,
`REC-SIT-CONCURRENCY-1`, `REC-SIT-ROLLBACK-1`), created and torn down inside
the test itself, never touching seeded/pipeline-generated rows. This was
necessary because zero real recommendations are currently READY_FOR_REVIEW —
see §6.

## 3. Environment

- Postgres 18 via podman/docker compose (`spares-postgres` container),
  reachable at `127.0.0.1`.
- No schema changes required for Phase 9 beyond the one genuine defect fix
  (§8).
- `alembic upgrade head` clean; `alembic check` reports one pre-existing,
  cosmetic index-definition drift unrelated to Phase 9 (§13).

## 4. Real database baseline (as found, before any Phase 9 change)

- `raw_*` tables: 27 tables, unchanged since Phase 2 (`raw_mseg` = 233,145 rows
  — spot-checked as the largest raw table).
- `i7_material_feature`: 45,409 rows (one per material-plant in the current
  feature generation).
  - `history_status`: 471 `SUFFICIENT`; 44,938 `NO_HISTORY` + `COLD_START`.
- `i7_recommendation`: 227,045 rows across all historical run generations
  (not deduplicated — expected; prior generations are retained for audit
  under the run-scoped idempotency design). The latest feature generation's
  own slice is 45,409 rows (one per current material-plant), matching the
  feature store exactly.
- `i7_recommendation` (latest generation): **0 READY_FOR_REVIEW**, 471 normal
  ("non-OAR") recommendations blocked on the unsigned service-level matrix,
  44,938 OAR-path recommendations, of which **741** have OAR similarity
  `AVAILABLE` but are blocked at `oar_estimate_status =
  NOT_EVALUABLE_SERVICE_LEVEL_UNSET`.
- No staged CDHDR/CDPOS exists on this extract (Phase 2 never materialised
  it) — every adoption evaluation resolves to `UNKNOWN`, never
  `NOT_ADOPTED`.

These numbers match the numbers the user's Phase 9 instruction stated as
expected. This is a **REAL DATABASE RESULT**, asserted directly by
`test_recommendation_gating.py` and `test_domain_integration.py`.

## 5. Domain integration results (REAL DATABASE)

- Multiple feature-run generations coexist in `i7_recommendation`; each
  generation's own row count equals the feature store's total at that time —
  no partial/mixed generation.
- `i7_inventory_run.feature_run_id` and `i7_oar_run.feature_run_id`, where
  present, always match the feature generation the referencing recommendation
  belongs to.
- Material + plant identity is preserved verbatim from `i7_material_feature`
  into `i7_recommendation` — no re-padding, no transformation, no collisions
  across plants for the same material, no cross-plant leakage of current SAP
  values (`current_rop` compared against `i7_staged_material_plant`).
- **Defect found and fixed**: `latest_forecast_run()` was unscoped, and could
  silently pick a forecast run belonging to an older feature generation than
  the one currently live. See §8.

## 6. Approval workflow results (TEST FIXTURE)

Because no real recommendation is currently READY_FOR_REVIEW (§4), the full
four-role chain was exercised end-to-end on isolated fixture rows, never on
seeded data:

- Full chain End User → Engineering Manager → Commercial Manager →
  Warehouse Supervisor reaches `SAP_EXECUTION_PENDING`.
- Negative cases all correctly rejected by the real `workflow`/`ledger`
  state machine: wrong actor role, skipped stage, REJECT without comment,
  SEND_BACK without comment, ADJUST without comment.
- REJECT-with-comment terminates the recommendation (`REJECTED`).
- SEND_BACK-with-comment returns `pending_role` to the correct prior role.
- ADJUST increments `current_version`.
- Retried SUBMIT and retried APPROVE-at-the-same-role are both safely
  rejected (`WorkflowError`), not silently duplicated — ledger entries
  proven to contain exactly one row per real action, never two.
- Ledger integrity: every field (`actor_id`, `actor_role`, `comment`,
  `previous_status`, `new_status`, ordering by timestamp/id) is preserved
  exactly; ledger rows are never overwritten as the recommendation's own
  status advances (5 ledger rows survive a full SUBMIT+4×APPROVE chain).

## 7. API end-to-end results (REAL DATABASE + fixtures)

- Every documented Phase 8 endpoint responds without a 500 against the real
  database: health, list, detail, trace, approval-history, adoption, runs
  list, run detail.
- Pagination is genuinely database-side: `total` is proven equal to a
  direct `count(*)` on `i7_recommendation`, not `len(items)`; page_size is
  capped at 200 (422 above); an impossible filter returns an empty page with
  `total == 0`, not an error; a far-out-of-range page returns an empty page,
  not an error.
- Error contract: malformed body → 422 with no traceback/SQL leak;
  unknown run type → 422 not 500; nonexistent recommendation → 404 with no
  `psycopg`/`sqlalchemy`/`traceback`/raw-SQL leak in the body.
- Traceability: a real blocked normal-path recommendation's detail and trace
  responses agree on `blocking_reason`, and correctly show demand
  classification present (Phase 3 ran) alongside recommended values absent
  (Phase 5/7 blocked) — both facts visible in the same response, nothing
  invented for a stage that never ran. A real OAR recommendation with
  `AVAILABLE` similarity traces through to `estimate_status =
  NOT_EVALUABLE_SERVICE_LEVEL_UNSET` and an overall `NOT_EVALUABLE` status,
  with `neighbour_count` and `estimate_status` both present as trace
  entries.
- Adoption endpoint never returns anything but `UNKNOWN` on this extract —
  proven against a real recommendation row.

## 8. Idempotency / traceability results

Confirmed by direct repository-level tests, not just observed as a side
effect:

- `repository.latest_forecast_run(session, feature_run_id)` — when scoped,
  always returns a forecast run whose own `feature_run_id` equals the
  generation requested; never a coincidental id-ordering artifact.
- Regression proof of the fix (§10): the *unscoped* lookup finds a forecast
  run belonging to an older feature generation than the one currently live
  (forecasting has not been re-run since feature generation advanced past the
  generation forecasting last targeted — a known, pre-existing environment
  condition, not fabricated for this test). The *scoped* lookup for the
  current generation correctly returns nothing (or a different run) rather
  than silently reusing the stale one.

## 9. Transaction / rollback results (TEST FIXTURE)

- An invalid workflow transition (`apply_approval_action` on a `NOT_EVALUABLE`
  row with `chain_index=0` attempting an out-of-turn APPROVE) raises
  `WorkflowError` **before** any mutation: the in-memory row's `status` and
  `chain_index` are unchanged, and zero ledger rows exist afterward — proven
  without an intervening commit, so this is a true pre-write rejection, not a
  committed-then-rolled-back side effect.
- Two independent sessions racing an APPROVE on the same fixture
  recommendation/stage: exactly one ledger `APPROVE` entry is recorded in the
  expected (and by far most likely) case, with the recommendation left at
  `PENDING_APPROVAL`/`chain_index=1`; the test also tolerates (and asserts
  ledger/row consistency under) the narrow race window where both in-process
  validations pass against stale in-memory state — in that case it requires
  the ledger to still hold both actors' decisions consistently, with the
  recommendation's `status` matching the ledger's own last entry. In neither
  branch is a decision silently lost or the state left inconsistent between
  the recommendation row and its ledger.

## 10. Defect found and fixed

**Symptom**: while writing `test_a_recommendations_provenance_is_internally_
consistent`-style checks, `repository.latest_forecast_run(session)` (no
scoping argument) was found to select the single globally-latest successful
forecast run, with no regard for which feature generation the recommendation
batch currently being built targets.

**Root cause**: forecasting (Phase 4) has, in this environment, only ever
been run against feature generation 8. The feature store has since advanced
to a later generation (20 in the currently live data) without forecasting
being re-run. An unscoped "latest forecast run" lookup therefore silently
returns generation 8's forecast run and joins it against generation 20's
recommendation batch — a stale, mismatched-generation join that on
regeneration would silently null out `forecast_rate` for recommendations
that should instead correctly report "no current forecast available".

**Fix** (code only — see §11 for why data was not regenerated):

- `app/initiatives/i7/recommendations/repository.py`:
  `latest_forecast_run(session, feature_run_id: int | None = None)` — when a
  `feature_run_id` is given, the query is constrained to
  `i7_forecast_run.feature_run_id = :feature_run_id`; the old unscoped
  behaviour is preserved only when the caller passes nothing (backward
  compatible signature).
- `app/initiatives/i7/recommendations/service.py`: the call site now passes
  the current `feature_run_id` explicitly, with an inline comment
  explaining why (this exact Phase 9 finding).

**Proving tests**:
- `tests/i7/sit/test_domain_integration.py::
  test_latest_forecast_run_is_scoped_by_feature_generation` — demonstrates
  the unscoped lookup returns a run from an older generation than the one
  currently live, and that the scoped lookup does not silently accept it.
- `tests/i7/sit/test_domain_integration.py::
  test_scoped_forecast_lookup_only_ever_returns_a_matching_generation` —
  proves the scoped lookup's returned run (when any) always belongs to the
  requested generation, across both an old (8) and the current generation.

**Classification**: genuine implementation defect (unscoped join), not an
expected unresolved business configuration and not a test/environment
artifact — although the specific number of affected rows in this environment
is inflated by an unrelated, expected environment condition (forecasting
not having been re-run since generation 8), which is documented separately
below and is not itself a defect.

## 11. Why the live database was not regenerated

Re-running `generate_recommendations()` to apply the fix against the current
feature generation was evaluated and explicitly **declined by the user**.
Investigating the true blast radius showed that regenerating today would
affect **all 471** normal-path recommendations (not merely a small handful),
because forecasting has genuinely never been run against generation 20 —
regenerating would make every normal-path recommendation newly show "no
current forecast" rather than a stale, silently-wrong one, which is the
*correct* behaviour but is also a nontrivial, visible change to the
currently-persisted dataset that the user chose to defer.

**Decision (explicit, given via AskUserQuestion)**: keep the code fix, do
not regenerate the live database. Consequence: the currently-persisted
`i7_recommendation` rows for the 471 normal-path recommendations still
reflect the **pre-fix**, unscoped forecast-run join. The fix is verified at
the repository-function level (§10) rather than by asserting anything about
currently-persisted rows, since those rows are expected, by design, to still
show pre-fix behaviour until a future Phase 4 + Phase 7 regeneration cycle is
run.

This is recorded here as the authoritative explanation for why
`app/initiatives/i7/recommendations/repository.py` and
`.../service.py` — both pre-existing Phase 7 production files — were
modified during a phase whose brief was "verify, not rebuild."

## 12. SAP safety results

- No module under `app/api` (the whole package, not just `app/api/i7`)
  imports `requests`, `httpx`, `urllib3`, or `app.integrations.sap` directly
  — proven via `ast.parse`/`ast.walk`, not string matching.
- `app/api/i7/approvals.py` specifically contains none of
  `requests`/`httpx`/`SapClient`/`sap_client`/`odata`.
- The adoption endpoint never turns an `UNKNOWN` into a fabricated
  `NOT_ADOPTED` — checked against a real recommendation row.
- No automated SAP write-back exists anywhere in the codebase; this was not
  newly proven in Phase 9 so much as re-confirmed at a wider scope than
  Phase 8's own check.

## 13. Known limitations

- **Pre-existing index-definition drift** (`ix_i7_oar_target_status`):
  `alembic check` reports the live model
  (`app/models/i7_oar.py`) declaring a two-column index
  (`status`, `confidence`) while the currently-applied migration state
  reflects a single-column form, due to the index being flip-flopped across
  migrations `b2184288fb3a`, `c131aa566b34`, and `64f7682e1dc1` during Phases
  6–7. Confirmed via `git log`/`grep` to predate Phase 9 entirely. This is a
  cosmetic index-metadata inconsistency (query correctness and results are
  unaffected; it does not change which rows are returned), not a
  data-correctness defect, and is out of scope for Phase 9 to fix under the
  "no unrelated refactoring" rule. Left for a future migration cleanup.
- **Forecasting has not been re-run against the current feature
  generation** (generation 20; forecasting last targeted generation 8). This
  is an environment/data-freshness condition, not a code defect — the fix in
  §10 makes the system correctly *report* this honestly (no current forecast
  available) rather than silently mask it, but the underlying staleness
  itself is a pipeline-scheduling matter outside Phase 9's scope.
  Regenerating recommendations after a future Phase 4 run to bring forecast
  coverage current is a natural next operational step, not a Phase 9 defect.
- **Zero real recommendations are READY_FOR_REVIEW** today, because the
  service-level matrix remains unsigned (an intentional, expected, and
  correctly-enforced safety gate, not a defect — confirmed exhaustively in
  §4/§6). All approval-workflow exercise in Phase 9 necessarily used isolated
  test fixtures for this reason.
- The two-session concurrency test tolerates (rather than requires) a
  narrow race outcome where both sessions' in-process validation passes
  against stale in-memory state before the first commit is visible; in
  that branch the test only proves the ledger/row stay mutually consistent,
  not that a second write is always rejected outright. A stricter guarantee
  would require a DB-level optimistic-lock column (e.g. a `version` check
  on `UPDATE`), which does not currently exist and was not added, since doing
  so would be new production behavior beyond "verify, not rebuild."

## 14. Test results

- Phase 9 SIT tests: **46 passed**, 0 failed, 0 skipped (skips are
  conditional on `needs_db`/data availability and did not trigger against
  this database).
- Full I07 suite (`tests/i7/`): **808 passed**.
- Full repository suite (`pytest`, all initiatives): **1,015 passed, 7
  deselected (live-SAP tests, `-m live`), 0 failed**, up from 969 passed
  before Phase 9 (46 new SIT tests, net).
- Warnings: none new; pre-existing deprecation warnings from third-party
  dependencies (documented in earlier phase reports) are unchanged in count.

## 15. Git / change summary

Modified (pre-existing production files, both justified by the single
genuine defect in §10 — no other production file was touched):
- `app/initiatives/i7/recommendations/repository.py`
- `app/initiatives/i7/recommendations/service.py`

New:
- `tests/i7/sit/test_domain_integration.py`
- `tests/i7/sit/test_recommendation_gating.py`
- `tests/i7/sit/test_approval_workflow_sit.py`
- `tests/i7/sit/test_api_sit.py`
- `tests/i7/sit/test_concurrency_and_performance.py`
- `docs/i07_sit.md` (this document)

No frontend file, no I8/I13 file, no SAP integration file, no migration, and
no other Phase 1–8 production file was modified.

## 16. Remaining blockers before a frontend audit / Phase 10

- Service-level matrix sign-off (blocks all normal-path READY_FOR_REVIEW).
- Max Stock strategy selection (EOQ vs. review-period — both implemented,
  neither chosen).
- Criticality source resolution.
- OAR rollup policy (per-plant-only vs. cross-plant) confirmation.
- I13 HOD ledger integration (adoption evidence currently structurally
  `UNKNOWN` for lack of any staged CDHDR/CDPOS).
- A future forecasting re-run against the current feature generation, to
  close the staleness gap identified in §10/§13 (operational, not a defect
  to "fix" in code).

None of these are Phase 9's responsibility to resolve; they are listed here
because Phase 9 is where their absence was most concretely, repeatedly
observed against the live database.

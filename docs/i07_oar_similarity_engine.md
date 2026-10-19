# I07 OAR donor similarity engine (`oar-similarity-2`)

How Phase 6 finds donor materials for a cold-start target, what changed in
`oar-similarity-2`, how to configure it, and how to calibrate it. Code lives in
`app/initiatives/i7/oar/`.

## What it does

A cold-start target is a material-plant that Phase 3 routed to `NO_HISTORY` or
`COLD_START`, so it cannot be forecast from its own history. Phase 6 finds the
materials it most resembles. If enough of them qualify, it borrows a
similarity-weighted Safety Stock, ROP and Max Stock from their current SAP
values.

```
target --> hard eligibility gates --> Stage A shortlist --> Stage B exact score
       --> admission (0.60 floor + evidence rule) --> Top-5 --> confidence
       --> estimate (>= 3 qualifying neighbours; every donor has SAP SS+ROP+Max > 0)
```

## Business rules (unchanged unless stated)

| Rule | Value | Where |
|---|---|---|
| Same, known criticality | hard gate | `eligibility.py` |
| Same material type (MTART) when both are known; blank on either side does not block (`oar-similarity-5`, 2026-10-19) | hard gate | `eligibility.material_types_conflict`, mirrored in `retrieval.py` |
| Known active | hard gate | `eligibility.py` |
| Known history >= 12 months | hard gate | `SimilarityPolicy.minimum_neighbour_history_months` |
| Not the target itself | hard gate | `eligibility.py` |
| Admission floor | combined >= **0.60** | `SimilarityPolicy.minimum_similarity` |
| Minimum evidence | **new**, see below | `SimilarityPolicy.minimum_evidence_features`, `identity_evidence_features` |
| Neighbours kept | top **5** (was 10, changed 2026-10-19) | `SimilarityPolicy.maximum_neighbours` |
| Estimate needs | >= **3** qualifying neighbours (was 5) | `SimilarityPolicy.minimum_neighbours` |
| Donor gate | current SAP SS, ROP **and** Max all > 0; those SAP values are lent (was: Phase 5 SS, ROP and Max all `SUCCESS`) | `repository.load_candidate_population`, `load_inventory_values` |
| Estimate | `sum(s_i * v_i) / sum(s_i)`, rounded up once | `estimate.py` |

A missing criticality, an unknown active status or unknown history is a
rejection, never a pass. The gates are applied before anything is scored.

## Previous vs new retrieval flow

**Before (`oar-similarity-1`).** For every target the code ran
`eligibility.filter_candidates` over the whole donor population, then the exact
`Decimal` score for every eligible donor. That included a pure-Python 384-value
cosine per pair, and per-pair embedding lookups. Work grew with targets × donors.
Every result row was held in memory until one insert at the end.

**Now (`oar-similarity-2`).** Two stages:

1. **Partition.** Donors are split once per run by criticality, keeping only
   those that pass the candidate-side gates (known active, known history >= 12
   months). Rejection counts per target are worked out from counts per
   criticality (`retrieval.PopulationProfile`). They are identical to
   `filter_candidates`, and a test checks that.
2. **Stage A, shortlist.** Every eligible donor in the target's partition gets a
   vectorised pre-score. It uses the same formula as the exact score, the same
   features and the same missing-feature handling, with the evidence rule
   included. Three channels then pick candidates, and the shortlist is their
   union with duplicates removed:
   * **structured**: best by structured + business pre-score (material group,
     unit of measure, manufacturer, price);
   * **semantic**: best by description cosine. This is an exact inner-product
     search over unit-normalised float32 embeddings, one matrix product per
     batch of targets per partition;
   * **fused**: best by the full combined pre-score, among pairs that pass the
     evidence rule.
3. **Stage B, exact.** Only the shortlist gets the exact `Decimal` score, using
   the same `_score_pair` the exhaustive path uses. Ranking, the 0.60 floor,
   Top-10, confidence, the 5-neighbour gate and the estimate then run through
   the same `finalise_target`.

Partitions at or below `I7_OAR_EXHAUSTIVE_BELOW` donors are scored exhaustively.

**Why the shortlist doesn't lose neighbours.** The fused channel ranks by the
same formula as the exact score, so the true Top-10 falls inside its pool
whenever the pool is at least 10 and floating point agrees with `Decimal` at the
six decimals scores are stored to. Ties are broken the way ranking breaks them:
material, then plant, ascending. This is tested directly. Two-stage and
exhaustive produce identical neighbours, scores, confidence and estimates on
seeded random populations, with and without embeddings, under three policies
(`tests/i7/test_oar_retrieval.py`). The benchmark measures the same thing at
scale.

**Why not FAISS, HNSW or a vector database.** A partition holds one criticality
class, with donors in the thousands. An exact flat index over a float32 matrix
is faster to build and query at that size, loses no recall, and adds no
dependency or service. The matrix sits behind `DonorPartition`, so an
approximate index could replace it if a partition ever outgrows matrix products.

## The score

For each dimension available for a pair:

```
S_struct   = 1 - mean(categorical distances over features known on BOTH sides)
             features: material_group (MATKL), equipment_type, base_unit_of_measure (MEINS)
S_text     = clamp(cosine(embed(target), embed(candidate)), 0, 1), rounded to 6 dp
S_business = 1 - mean(distances over manufacturer (MFRNR), price proximity)
             price distance = min(|p_a - p_b| / population price range, 1)

S = sum(W_d * S_d for available d) / sum(W_d for available d)
    W_struct = 0.35, W_text = 0.30, W_business = 0.35   (SimilarityPolicy, unchanged)
```

All components are in [0, 1].

* **Missing values never match.** A feature counts only when both sides have
  it. Two missing material groups are "not compared", not "the same".
* **A dimension with nothing to compare is excluded and the weights
  renormalise.** It is not scored 0. `score_completeness` records how much of
  the intended weight was computable.
* **No dimension computable** means the score is `None` and the pair is never
  admitted.

### Minimum evidence (new)

Renormalising lets a single weak feature produce 1.0. Two parts that share only
the unit of measure "EA" score 1.0 on that alone, and would outrank genuine
matches. A pair is therefore admitted only if:

* at least `minimum_evidence_features` (default **2**) features were compared,
  and
* at least one of `identity_evidence_features` (default **material_group,
  description**) was compared. These say what a part *is*; unit of measure and
  price only say how it is counted and what it costs.

A refused pair keeps its score for explanation, isn't admitted, and is counted
in `i7_oar_target.low_evidence_candidates`. Setting
`minimum_evidence_features=0` and `identity_evidence_features=()` restores the
old behaviour.

**This is a deliberate change in what can be a neighbour.** It is based on the
rule above, not on measured accuracy. Review it with the business before relying
on it in production.

### Explanations

Every stored neighbour carries `score_explanation`, for example:

```
used=material_group,base_unit_of_measure,unit_price;missing=equipment_type,description,manufacturer;
weights=structured:0.5000,business:0.5000;text=NOT_AVAILABLE_NO_MODEL;evidence=ok
```

The explanation names the features compared, the features that couldn't be
compared, the renormalised weights actually applied, the text status, and the
evidence verdict. With the stored dimension scores, that is enough to recompute
the combined value.

## Circuit

Circuit (Crushing, Milling and so on) has **no trustworthy source** in the SAP
data delivered so far. Since `oar-similarity-2`:

* it is not part of the business score (no bonus, no penalty);
* it is not a ranking tie-break;
* two missing circuits are never read as "the same circuit";
* `same_circuit` on neighbours is always `NULL`.

The field stays on `CandidateAttributes` and in `i7_oar_neighbour` for
compatibility. Re-enabling it needs a real source first, then adding it back to
`BUSINESS_CATEGORICAL_FEATURES` and `SIMILARITY_FEATURES`.

**Not changed: the confidence rule.** `confidence.grade` still requires at least
one same-circuit neighbour for **HIGH**. With no circuit data, HIGH stays out of
reach, as it already was. Changing that changes what HIGH means to its
consumers, so it is left for a business decision.

## Text similarity and embeddings

* Model: `all-MiniLM-L6-v2` through `sentence-transformers`. **This is optional
  and not in `requirements.txt`**, because it pulls in torch. Without it, the
  text dimension reports `NOT_AVAILABLE_NO_MODEL` and scores renormalise over
  structured and business. That is the state of every environment today.
* **One embedding per unique normalised description per model version.** The
  layers are the per-run memory map, then `i7_oar_embedding` in Azure SQL,
  then the model in bounded batches (`I7_OAR_EMBEDDING_BATCH_SIZE`).
* **Cache key** = SHA-256 of (normalised description, model name, model
  version, `PREPROCESSING_VERSION`). A changed description, model or
  normalisation rule produces a new key, so a stale vector is never looked up.
  No manual invalidation is needed.
* **The model is loaded once per process** (module-level cache), and a failed
  load is remembered rather than retried.
* **Housekeeping:** `SqlEmbeddingStore.prune_other_versions(model, version)`
  reclaims rows from other versions. `SqlEmbeddingStore.clear()` is a full
  rebuild; the next run regenerates what it needs.
* Vectors are stored as little-endian float32 bytes (`LargeBinary`), not text.

## Configuration

Retrieval settings only change how much work a run does. Business rules stay in
`SimilarityPolicy`.

| Setting | Default | Meaning |
|---|---|---|
| `I7_OAR_RETRIEVAL_MODE` | `two_stage` | `exhaustive` scores every eligible donor exactly |
| `I7_OAR_STRUCTURED_POOL` | 50 | structured channel size |
| `I7_OAR_SEMANTIC_POOL` | 50 | description channel size |
| `I7_OAR_FUSED_POOL` | 100 | combined channel size (must be >= K) |
| `I7_OAR_EXHAUSTIVE_BELOW` | 200 | partitions this small are scored exhaustively |
| `I7_OAR_TARGET_BATCH_SIZE` | 256 | targets per matrix product |
| `I7_OAR_EMBEDDING_BATCH_SIZE` | 64 | descriptions per model call |
| `I7_OAR_WRITE_BATCH_SIZE` | 2000 | target results inserted per batch |
| `I7_OAR_EMBEDDING_CACHE` | `true` | keep embeddings in `i7_oar_embedding` |

Each run records `retrieval_config`, and it is part of the run reuse key.

## Calibration (threshold and weights)

The 0.60 floor and the 0.35/0.30/0.35 weights are the Solution Design's starting
values. They have **not** been calibrated against reviewed material pairs, and
this work doesn't change them. To calibrate:

1. **Sample targets.** Draw about 50 cold-start targets, stratified by
   criticality and material group.
2. **Build the review list.** For each target, take the engine's top 10 plus a
   few random eligible donors, so reviewers don't only see what the engine
   already liked.
3. **Collect expert judgements.** An inventory or maintenance engineer marks
   each pair: could this part's stocking history stand in for the target's?
   Record the answers in the CSV format in `oar/evaluation.py`.
4. **Measure, then propose.** Score the reviewed pairs, then run
   `evaluation.threshold_sweep` and `evaluation.recommend_threshold` (lowest
   floor meeting a required precision on enough pairs), plus `precision_at_k`
   and `recall_at_k` per configuration.
5. **Apply as a reviewed policy change.** Change `SimilarityPolicy`, bump the
   policy version, and record the measurements with the change. Nothing in the
   code moves the floor automatically.

## Benchmark

`python -m scripts.benchmark_oar_similarity [--legacy-root <checkout of main>]`
runs a deterministic synthetic catalogue (100,000 materials by default). It
reports:

* embedding generation and the repeat-run hit rate;
* index build time;
* Stage A latency over every target;
* end-to-end latency for structured-only and hybrid retrieval;
* exhaustive timing for both the new and the current production code;
* how often two-stage reproduces the exhaustive Top-10;
* synthetic precision@5 and @10;
* peak memory.

Synthetic precision only shows that the engine uses its signals correctly. It is
**not** a measure of real matching accuracy.

### Results, 2026-10-12

The run used 100,000 synthetic materials: 25,000 donors and 75,000 targets, with
18,964 unique descriptions and 384-dimension stand-in vectors. Hardware was a
Windows 11 laptop with 8 logical CPUs, numpy 2.5.3 and Python 3.14. No real
embedding model was used.

| Measure | Result |
|---|---|
| Eligible-donor partitions | NORMAL 13,382 · IMPACT 3,382 · CRITICAL 2,264 · INSURANCE 1,100 |
| Index build | 0.31 s |
| Embeddings, first run | 18,964 generated (one per unique description) |
| Embeddings, repeat run | **0 generated**, hit rate 1.0 |
| Stage A, all 75,000 targets | 64.7 s (0.86 ms per target), shortlist mean 175, max 200 |
| Two-stage hybrid, end to end | **7.35 ms per target** (2,000-target sample) |
| Two-stage structured-only | 3.71 ms per target |
| Exact scores computed | 1.7% of eligible pairs (hybrid), 1.35% (structured-only) |
| New exhaustive mode | 396 ms per target (40-target sample) |
| **Current production code**, exhaustive | **658 ms per target** (20-target sample) |
| Two-stage Top-10 identical to exhaustive | **40 of 40** targets; recall of exhaustive neighbours 1.0 |
| Peak resident memory | 332 MB (whole 100k catalogue in process) vs 322 MB legacy |

Extrapolated to all 75,000 targets: about 9 minutes for two-stage, against about
14 hours for the current code. The extrapolation is linear from the samples.

Synthetic precision@5 on the same 20 targets was 0.16 for the current code and
0.31 for the new engine. **The gain comes from the evidence rule, not from
retrieval.** The current code's top neighbours included donors with no material
group and no description. Their 0.98 rested on unit of measure "EA" plus a
nearby price, which the new rule refuses.

## Known limitations

* **Real accuracy is unvalidated.** There are no expert-labelled pairs yet.
* **No real text similarity anywhere yet.** `sentence-transformers` is not
  installed, so the semantic channel and the text dimension have only been
  exercised with deterministic stand-in vectors.
* **Manufacturer is not delivered** in the CSV extract (MARA has no MFRNR),
  equipment type has no source, and circuit has no source. In practice, the
  structured evidence is material group, unit of measure and price.
* **Duplicate index name on `i7_oar_target`.** `status` has `index=True`
  (auto-named `ix_i7_oar_target_status`), and `__table_args__` declares an
  explicit two-column index with the same name. `metadata.create_all` cannot
  build the table. Migrations build it, so production is unaffected. This is
  pre-existing and not changed here.

# I07 OAR Similarity Engine (Phase 6)

Finds comparable materials for an OAR target and, where evidence permits,
derives a similarity-weighted inventory estimate.

**Targets (since `oar-similarity-4`, 2026-10-19):** every OAR material-plant --
MRP type ND / PD / blank -- whatever its history. A VB / other material is never
a target: it is forecast, or not evaluable. Earlier sections below describe the
cold-start-only target set and its measured results; read them as history.

```
OAR / Cold Start (Phase 3: NO_HISTORY or COLD_START)
       ↓
Hard Constraints
       ↓
Same Criticality?  →  Active?  →  ≥12 Months History?
       ↓
Eligible Candidates
       ↓
┌────────────────────────────┐   ┌────────────────────────────┐   ┌────────────────────────────┐
│ Structured Similarity      │ + │ Text Similarity            │ + │ Business Similarity        │
│ Gower                      │   │ BGE cosine + fuzzy ratio    │   │ Circuit / Price / OEM      │
└────────────────────────────┘   └────────────────────────────┘   └────────────────────────────┘
       ↓
Combined Score (renormalised over available dimensions)
       ↓
Rank  →  Top K
       ↓
Confidence (HIGH / MEDIUM / LOW)
       ↓
Similarity-Based Estimate (only from neighbours with a successful Phase 5 calculation)
       ↓
Human Review (Phase 7)
```

Consumes the Phase 3 history gate and OAR routing as given. Nothing here
recomputes them, and nothing here generates a recommendation — that is Phase 7.

## Running it

```bash
.venv\Scripts\python.exe -c "from app.initiatives.i7.oar import run_oar_similarity; print(run_oar_similarity())"
```

~14s for all 44,938 cold-start material-plants against a 471-material donor pool.

## Structure

| Module | Responsibility |
| --- | --- |
| `repository.py` | targets, candidate population, Phase 5 inventory lookups |
| `eligibility.py` | the three hard constraints |
| `structured_similarity.py` | Gower distance — material group, equipment type, UoM |
| `text_similarity.py` | BGE embeddings, pluggable, absent by default; the cache |
| `text_match.py` | description normalisation and the rapidfuzz half of the text score |
| `business_similarity.py` | circuit, price proximity, manufacturer |
| `scoring.py` | weighted combination with renormalisation |
| `identity.py` | identity validation: part numbers, component types, same-group evidence (`oar-similarity-10`) |
| `ranking.py` | deterministic Top-K |
| `confidence.py` | HIGH/MEDIUM/LOW grading |
| `estimate.py` | similarity-weighted SS/ROP/Max (weighted median by default) |
| `demand_check.py` | diagnostic: a proposed ROP against the target's own consumption |
| `service.py` | orchestration and persistence |

## OAR vs. Intermittent — not the same axis

INTERMITTENT means history exists and is irregular; OAR/cold-start means there
is insufficient or no usable history at all. Phase 6 handles only what Phase 3
already routed to `NO_HISTORY` or `COLD_START` — a `SUFFICIENT` material is
never pulled in here merely because its forecast looked uncertain.

## Candidate population

**Donors must themselves be classified.** A neighbour needs ≥12 months of
history, so the only material-plants eligible to be neighbours are the 471 Phase
3 routed to `SUFFICIENT` — the same 471 Phase 4 and Phase 5 already work with.
The other 44,938 are targets, never donors.

## Hard constraints — none is relaxed

| Constraint | Eligible | Rejected |
| --- | --- | --- |
| Criticality | same class, both known | different class; **either side unknown** |
| Active | `is_active = True` | inactive; **unknown** |
| History | ≥ 12 months | < 12 months; **unknown** |

Unknown is a rejection at every constraint, never a pass. Missing criticality
does not mean "probably the same class" — with 1.7% coverage a permissive
reading would pair almost every material with almost every other. A material is
never its own neighbour.

Hard filtering runs **before** any similarity is computed: embedding a
description costs far more than comparing two strings, and an ineligible
candidate must never influence a ranking.

## The three dimensions

**Structured (Gower).** Material group, equipment type, unit of measure.
Categorical distance is 0/1; numeric distance is `|a−b| / range`, with the range
taken from the whole candidate population, never from the pair being compared.
A feature missing on either side is **excluded**, not scored as a match — two
materials that both lack an equipment type have not been shown to be alike.

**Text (BGE + fuzzy, `oar-similarity-11`).** Two signals over the same normalised descriptions
(`oar/text_match.py`):

```
S_text = 0.6 x cosine(BGE embeddings)  +  0.4 x fuzzy
fuzzy  = (token_set_ratio + token_sort_ratio) / 2 / 100          (rapidfuzz)
```

* **Embedding**: `BAAI/bge-base-en-v1.5` through `sentence-transformers`, 768-dim, `normalize_embeddings=True`,
  CPU. It replaced `all-MiniLM-L6-v2`, which is no longer supported.
* **Fuzzy**: the average of rapidfuzz's `token_set_ratio` (ignores words only one side has) and
  `token_sort_ratio` (every extra word counts against the match). Both are word-order independent. Averaged, a
  short description that is a word-subset of a longer one is no longer a perfect match (`FILTER` against
  `FILTER PRESS ANDRITZ SPRAY NOZZLE`: set 100, sort about 31, average about 65), while a reordering of the same
  words still is.
* **Normalisation, before both**: upper-case, commas and extra spaces removed, then whole-word abbreviation
  expansion `BUSH`->`BUSHING`, `PL`->`PLATE`, `ASSY`->`ASSEMBLY`, `BRG`->`BEARING`. Only the text dimension sees
  the normalised text; the identity stage reads the original description.
* The weights are `SimilarityPolicy.text_embedding_weight` / `text_fuzzy_weight` (sum to 1; fuzzy 0 is the
  embedding alone). Both parts are kept on the score (`DimensionScore.components`) and appear in the stored
  explanation as `text_parts=embedding:..,fuzzy_set:..,fuzzy_sort:..`.
* **Retrieval.** The semantic channel takes the top 50 donors **by the embedding cosine alone** (the "top 50 by
  BGE"), and every shortlisted donor is then re-scored with the full formula. The pre-score mirrors the exact
  score (the fuzzy half is one rapidfuzz `cdist` per batch of targets).
* **FAISS is not used.** The semantic channel is an exact flat inner-product search already (numpy matrix
  product per criticality partition), which is what a FAISS `IndexFlatIP` returns; and the pre-score needs the
  text score of EVERY donor in the partition, which a top-k index cannot supply. `faiss-cpu` would add a native
  dependency for no gain; the index sits behind `DonorPartition` if a partition ever outgrows a matrix product.
* **Not installed by default.** `requirements-oar-text.txt` (torch, sentence-transformers) is optional, so without
  it every result reports `NOT_AVAILABLE_NO_MODEL` and the combined score renormalises over structured and
  business alone; `rapidfuzz` is in `requirements.txt`. No TF-IDF or token-overlap stand-in is substituted. Tests
  inject a fake provider so the suite never downloads a model.

**Two properties of this score to keep in mind.**
1. A subset still scores well (about 65 above), and even descriptions with no word in common score a few points
   (`PUMP SEAL` vs `ZZZ QQQ`: set 12.5). A high text score is therefore not evidence of the same part; the
   identity stage is.
2. **The identity floors were set for a MiniLM cosine and have not been calibrated for this score.** BGE cosines
   run higher and the fuzzy half adds to them, so 0.50 admits far more than it did.
   `SimilarityPolicy.text_floors_calibrated` stays `False` (and every run logs a warning) until they have been
   re-set and confirmed: `python -m scripts.oar_identity_review floors` derives the floor that keeps the old
   selectivity from the random controls of a review sample, and `evaluate` then sweeps floors against the
   expert's answers.

**Business.** Circuit, manufacturer (OEM proxy), price proximity over the
population range. Criticality does **not** appear here — it is a filter applied
before scoring, so every candidate reaching this dimension already matches, and
scoring it too would add an identical constant to every pair.

## Combined score

```
S = W1 x S_struct + W2 x S_text + W3 x S_business
W1 = 0.40, W2 = 0.40, W3 = 0.20 (SimilarityPolicy; 0.35 / 0.30 / 0.35 until oar-similarity-9)
```

**A missing dimension is excluded and the weights renormalise over what
remains** — not scored as 0. With text unavailable throughout this run, every
score in practice is `0.40/0.60·S_struct + 0.20/0.60·S_business`.
`score_completeness` records the fraction of intended weight actually used, so a
0.65 built from two dimensions is distinguishable from a 0.90 built from three.

## Identity must match (oar-similarity-8)

*Superseded in part by `oar-similarity-10`, below: the same-group route now needs description evidence.*

Admission needs at least two features compared **and** an identity match. Until `-7` the rule only asked
that material group or description had been *compared*; a neighbour in a different group with weak text
similarity still passed, lifted over the 0.60 floor by a similar price (the impact wrench's neighbours were
a blasting product, crusher bolts, a pipe wrench and a valve: other groups, text 0.31-0.46, combined
0.61-0.63). Now a neighbour must share the target's material group, **or** have a text similarity of at
least `SimilarityPolicy.minimum_identity_text_similarity` (0.50, a first setting to be checked against
known good and bad matches). Without an embedding model only the same-group route remains. A target left
with fewer than three neighbours gets no estimate, with the reason recorded. The exact score
(`scoring.combine`) and the two-stage retrieval (`retrieval.DonorIndex.prescore`) apply the same rule.

## Identity is a stage of its own (oar-similarity-10)

`oar-similarity-8` asked for an identity match but accepted **the same material group on its own**. Group
308175 holds filters, spray nozzles, proximity sensors and wrenches, so a target `FILTER PN:5112305242`
accepted `FILTER PRESS ANDRITZ SPRAY NOZZLE` and `FILTER PRESS ANDRITZ PROXIMITY SENSOR` as neighbours and
their ROPs went into the estimate. Identity is now validated by `oar/identity.py`, after scoring and before
ranking, in this order (the first rule that applies decides):

1. **Component conflict** -- both descriptions name a component type from the vocabulary and no pair is
   compatible (`filter` vs `nozzle`; `sensor` and `proximity_sensor` *are* compatible, one being a kind of
   the other): `REJECTED_COMPONENT_CONFLICT`. A description naming no vocabulary type is *unknown*, never a
   conflict. "FILTER PRESS" is context (the machine), so its FILTER is not read as a component type.
2. **Shared part number** (separators ignored; `55KW` and `100X50X6` are specifications, not part numbers):
   `CONFIRMED_PART_NUMBER`.
3. **Same material group** needs description evidence too: a compatible component type plus either text
   similarity >= `same_group_minimum_text_similarity` or a shared specific word
   (`CONFIRMED_GROUP_COMPONENT_TYPE`); or, with the type unknown, text similarity >= that floor
   (`CONFIRMED_GROUP_TEXT`) unless the descriptions share only generic words -- FILTER, PRESS, ANDRITZ
   (`INSUFFICIENT_GENERIC_OVERLAP`). Otherwise `INSUFFICIENT_GROUP_ONLY`. Two descriptions with no words in
   common are *not* generic overlap: differently worded equivalents stay eligible.
4. **Other or unknown group** -- text similarity >= `minimum_identity_text_similarity` (0.50, unchanged):
   `CONFIRMED_TEXT`, else `INSUFFICIENT_TEXT` / `INSUFFICIENT_NO_TEXT`.

Two identical descriptions count as text similarity 1.0 when no model is available. Identical wording is
otherwise never required.

**Auditable.** The verdict, the component types found, any shared part number and the text similarity travel
on `CombinedScore.identity`; a refused candidate keeps all its scores. `scoring.explain` (stored per
neighbour) ends `identity=<STATUS>`. `OarResult.rejected_candidates` keeps the ten best-scoring refusals with
their reasons. `i7_oar_target.rejection_summary` gains `IDENTITY_COMPONENT_CONFLICT=n` and
`IDENTITY_INSUFFICIENT_EVIDENCE=n`, counted over the whole eligible pool.

**The shortlist applies the same rule before it picks** (`retrieval.DonorIdentity`, vectorised). A donor the
identity stage refuses is invisible to the structured, semantic and fused channels alike, so it cannot take a
slot or re-enter through another channel. `tests/i7/test_oar_identity.py` holds the vectorised mask and the
per-pair rule to the same answer over 11,000+ generated pairs.

**Thresholds are configuration, not new decisions.** `SimilarityPolicy.same_group_minimum_text_similarity` is
0.50 -- the documented baseline, not a tuned number. The vocabulary (`identity_component_types`), the context
phrases and the generic terms are policy fields with conservative defaults in `oar/identity.py`.
`identity_validation=False` restores the `-8/-9` rule exactly, for before/after comparisons.
**Calibrating the floors needs expert-reviewed pairs scored with the real embedding model** (`oar.evaluation`);
neither exists in this repository, so the tests inject their similarities.

### Where the retrieval counts come from

For one target (`OarResult.stage_counts`, summed per run in the log):

| Count | Stage responsible |
| --- | --- |
| donor pool (e.g. 2,045) | `repository.load_candidate_population`: SUFFICIENT history and SAP SS, ROP and max all > 0 -- de-duplicated by material-plant |
| hard-filter passes (e.g. 1,598) | `eligibility`: same criticality, compatible material type, active, >= 12 months, not the target |
| structure / text / fused shortlists | the three channels of `retrieval.DonorIndex.shortlist` (50 / 50 / 100 by default) |
| merged (e.g. 149) | their de-duplicated union -- the only candidates scored exactly, because the partition is above `exhaustive_below` (200) |
| identity conflicts / insufficient | `oar.identity`, over the whole eligible pool |
| identity passes, score-floor passes, final | exact scoring, `minimum_similarity` (0.60), Top-K (5) |

## Ranking and Top-K

Only candidates that pass identity **and** the score floor are ranked. Deterministic: similarity descending, then same-circuit, then material number,
then plant. Top-K comes from `SimilarityPolicy`: at most **5** neighbours, and an
estimate needs at least **3** (changed 2026-10-19 from 5–10, a business override
of the documented K = 5–10). Fewer eligible candidates than K yields fewer
neighbours — nothing is padded.

## Confidence

Formula Reference bands, read exactly: HIGH needs **>** 0.80 similarity and
**≥** 5 same-circuit neighbours; LOW is **<** 0.60 or < 3 neighbours; 0.60 and
0.80 themselves fall in MEDIUM. Confidence is a business-quality judgement, not
the similarity score — a single 0.97 neighbour is an excellent match and still
LOW confidence, because one donor is not a basis for a stocking parameter.

**HIGH is unreachable on this extract**: no circuit data exists anywhere in the
27 raw tables, so the same-circuit condition can never be satisfied.

## Similarity-weighted estimate

```
SS_oar = weighted_median(SS_i ; weight s_i)     (same form for ROP, Max)
Max_final = max(Max_oar, ROP_oar + SS_oar)
```

**The estimator is a similarity-weighted median** (`oar-similarity-10`, `SimilarityPolicy.estimator`); the
Formula Reference's weighted mean `sum(s_i x v_i) / sum(s_i)` remains selectable as `weighted_mean`. A mean is
dragged by one extreme donor: four filter donors with ROP 3, 9, 6, 3 and a fifth with ROP 61 (similarities
0.90, 0.80, 0.75, 0.70, 0.65) give a mean ROP of 14.78 -> 15 and a median of **6**. The median is the first
value, walking up through the donors sorted by value, at which the accumulated similarity reaches half the
total; when it lands on exactly half it is the midpoint of that value and the next (computed in `Decimal`, so
the tie is exact). The result is rounded up as before and the Max floor below still applies. No donor is
removed for having a high value, and donor SAP values are never changed.

Both estimates are always computed. The trace records `estimator`, and for each parameter `<p>_mean`,
`<p>_median` and `<p>_material_difference` (a diagnostic flag: the two differ by at least
`estimator_material_difference_ratio` = 25% of the mean; it changes nothing). The recommendation's
`calculation_trace` carries them. Each donor's weight and lent values are the neighbour rows already stored.
`python -m scripts.compare_oar_estimators` re-derives both estimates from a stored run's neighbours.

## Target-demand sanity check

`oar/demand_check.py` -- a **diagnostic**, never a gate, and kept apart from similarity and eligibility. It is
not evidence of identity (a quiet target can still have the right neighbours) and it does not touch the
approved OAR conversion usage rule. It compares a proposed ROP with what the target's own consumption calls
for over its lead time, `mean x LT + z x sigma x sqrt(LT)` (z = 1.645), and flags `HIGH_VS_DEMAND` above 2x
that (both `DemandSanityPolicy`, first settings for a diagnostic). A ROP above *annual* consumption is not by
itself a flag. The window is the 12 months ending at the latest month the **source data** holds, never the
system date. `INSUFFICIENT_DATA_*` (no history, incomplete window, no lead time, no variability) is reported
instead of inventing a value, and no history is *not* zero consumption: `ZERO_CONSUMPTION` needs a complete
window with nothing consumed. It appears in the OAR recommendation's `calculation_trace` as `demand_check_*`.

**Max stock consistency floor** (`oar-similarity-6`, approved deviation from the plain weighted
average, 2026-10-19). The three averages are independent, so Max could land at or below ROP. After all
three are calculated and rounded up, `Max_final` is raised to `ROP + safety stock` when the weighted Max
is below it. It applies only when ROP, safety stock and the weighted Max are all present -- a missing
value is never filled in. ROP and safety stock are never changed. Stock items are not affected: their
Max comes from the Max Stock strategy (`inventory/max_stock.py`). The estimate's trace keeps the original
beside the result (`max_stock_weighted`, `max_stock_floor`, `max_stock_final`, `max_stock_floor_applied`,
`max_stock_floor_reason` = `MAX_RAISED_TO_ROP_PLUS_SAFETY_STOCK`), and the recommendation's
`calculation_trace` carries the same entries. Example: ROP 2, safety stock 1, weighted Max 2 -> Max 3.
On the 2026-10-19 run 4,824 of 10,093 OAR estimates were below the floor. No schema change: the entries
ride in the existing trace text.

Rounded **once**, after the weighted average — rounding each neighbour first
would compound several ceilings.

**A neighbour lends its current SAP values** (`oar-similarity-3`, 2026-10-19).
Only a SUFFICIENT-history material-plant whose current SAP safety stock, ROP
and max stock (MARC EISBE / MINBE / MABST) are **all above zero** is a
candidate at all, and those SAP values — rounded up to whole units — are what
it lends. I07's own Phase 5 calculation is never lent, even where it
succeeded. Because the candidate pool and the lent values share one
condition, every neighbour contributes to the estimate, and the 3-neighbour
minimum counts real contributors.

Previously a neighbour lent only a successful Phase 5 calculation; with the
service-level matrix unsigned that was none of them, so every estimate that
found neighbours reported `NOT_EVALUABLE_SERVICE_LEVEL_UNSET`.

Where an estimate does succeed, it is labelled `SIMILARITY-BASED ESTIMATE` —
never `FORECAST` or `ML PREDICTION`.

## Results on the July/August extract

| | Count |
| --- | --- |
| Cold-start targets | 44,938 |
| — with known criticality | 784 |
| Candidate population (donors) | 471 |
| — with known criticality | 4 |
| Targets with ≥1 eligible neighbour | 741 |
| Targets with 3–4 neighbours | 741 |
| Targets with ≥5 neighbours | 0 |
| Confidence: MEDIUM | 219 |
| Confidence: LOW | 44,719 |
| Confidence: HIGH | 0 (unreachable — no circuit data) |
| Estimate: `NOT_EVALUABLE_NO_NEIGHBORS` | 44,197 |
| Estimate: `NOT_EVALUABLE_SERVICE_LEVEL_UNSET` | 741 |
| Estimate: `SUCCESS` | **0** |

**Rejection profile** (aggregated): `CRITICALITY_MISSING_TARGET` dominates —
44,154 targets have no criticality at all and so cannot even attempt a match;
the remaining 784 targets with known criticality are checked against a donor
pool where only 4 candidates carry criticality, so most targets that do have a
criticality still find at most 4 possible donors.

## Example

Target `4000002855/1300` (known criticality) against the 4-candidate donor pool:
4 eligible, 4 neighbours, best similarity 0.6467 → **MEDIUM** confidence
(clears the 0.60 bar and the 3-neighbour minimum, but not HIGH's 5-neighbour or
same-circuit requirement). Structured similarity 1.0 (identical material group
and UoM) on the top three neighbours; text unavailable; business 0.14–0.29.
Estimate: `NOT_EVALUABLE_SERVICE_LEVEL_UNSET` — no Phase 5 value exists to
weight.

Negative case: target `1000000030/1300` — criticality unknown → 0 eligible
candidates → `NO_ELIGIBLE_NEIGHBORS`, confidence LOW, no fallback attempted.

## Formula verification

`S_struct`, `S_business` and the weighted estimate are checked against
hand-computed fixtures (e.g. `(0.90×10 + 0.80×20)/(0.90+0.80) = 14.7 → 15`); all
match exactly. `S_combined` renormalisation is verified against a controlled
three-dimension fixture: `0.40×0.80 + 0.40×0.60 + 0.20×0.90 = 0.74`, and with
text withheld, `(0.40×0.80 + 0.20×0.90)/0.60 = 0.833`.

## Idempotency

A run is identified by `(feature_run, inventory_run, policy_id, policy_version,
algorithm_version, embedding_model_version)`. Repeating with unchanged inputs
returns `reused_existing=True` instantly with no new rows.

## Performance

44,938 targets × up to 471 candidates each, scored in ~14s. Candidate population
and Phase 5 inventory values are loaded once per run (two queries), never once
per target. No all-pairs computation over the full 45,409-row catalogue — the
donor side is the 471-row classified population only.

## Limitations

**Criticality coverage is the binding constraint**, on both sides of the match:
784 of 44,938 targets (1.7%) and 4 of 471 donors (0.85%). Both trace to the same
MARA coverage gap Phase 2 identified. Fuller MARA/ZMM065 coverage is the single
highest-leverage fix for Phase 6's yield.

**Service-level matrix unsigned** blocks every weighted estimate — identical to
Phase 5's blocker, inherited rather than re-solved here.

**No circuit data** exists in the extract, so `same_circuit` is always unknown
and HIGH confidence is structurally unreachable.

**Text similarity unavailable** — `sentence-transformers` is not installed. The
combined score is currently structured + business only; installing the library
adds the third dimension with no code change.

**OAR roll-up remains unresolved**, per-plant only, unchanged from Phase 3.

**OAR → Min-Max conversion is not implemented here** — Phase 7's job, per the
Solution Design's explicit separation of identification from conversion.

# I07 Staging (Phase 2)

Turns the seeded July/August extract into source-independent canonical data.

```
raw_*  ──→  extract adapter  ──→  i7_staged_*  ──→  canonical contracts  ──→  Phase 3
(immutable)                       (this phase)      (repository.py)
```

`raw_*` is read only. Nothing in Phase 2 writes to it, renames it, or cleans it
in place — it stays the reconciliation baseline for the live-OData cutover.

## Running it

```bash
.venv\Scripts\python.exe -c "from app.initiatives.i7.adapters import stage_extract; print(stage_extract())"
```

~50s for the full extract. Idempotent: run it twice and the staged state is
identical.

## Structure

| Module | Responsibility |
| --- | --- |
| `adapters/field_map.py` | extract column → SAP field → canonical name, and the SAP codes |
| `adapters/ingestion_policy.py` | configurable choices (movement types, batch size) |
| `adapters/validation.py` | parsing, rejection reason codes |
| `adapters/extract.py` | raw → staging. **The only module that reads `raw_*`** |
| `adapters/repository.py` | staging → canonical contracts, for Phase 3 |

A boundary test fails the build if anything outside `adapters/` names a raw
table, so Phase 3 cannot accidentally reach past the contract.

## Staged tables

| Table | Rows | Natural key | From |
| --- | --- | --- | --- |
| `i7_staged_material` | 5,904 | `sap_material_number` | MARA + MAKT + ZMM065 + MBEW |
| `i7_staged_material_plant` | 45,409 | material + plant | MARC |
| `i7_staged_consumption` | 13,736 | material + plant + period | MSEG |
| `i7_staged_purchase_order` | 77,493 | PO document + item | EKPO + EKKO + EKBE + EKET |
| `i7_staging_rejection` | 1,171 | — | any |
| `i7_staging_run` | one per run | — | — |

Every natural key carries a unique constraint, and writes use
`ON CONFLICT DO UPDATE`. That is the one deliberate exception to the
repository's no-dialect-specific-SQL rule: idempotency has to be a database
property, because an application-level existence check races with a concurrent
run. Porting to another engine means changing `_upsert` alone.

## Decisions worth knowing

**Consumption is aggregated to months in SQL.** 233k movement rows collapse to
13,736 monthly totals; pulling them into Python to sum would move a lot of data
to no purpose. Issues count positive and reversals negative via `SHKZG`, so a
cancelled issue nets out rather than inflating demand.

**Zero-demand months are not stored.** A zero row is the *absence* of a
movement, so materialising every month for every material-plant would be a large
mostly-empty table. `repository.consumption_series_for()` densifies over each
material's own observed range instead — which matters because `ADI = n / n_nz`
counts total periods, and a dropped empty month misclassifies intermittent
demand as smooth. The contract rejects a gap rather than tolerating one.

**Lead time is stored unfiltered.** `lead_time_days` is the plain date
difference. The 1–730 day window and the PO-count tiers are Phase 3 policy;
applying them here would freeze a threshold into stored data where it could not
be changed without re-staging. Receipts dated before their order are rejected
(1,171 of them) rather than stored as negative durations.

**The movement-type set is configurable and unconfirmed.** 201/261 issues and
202/262 reversals, per `ConsumptionMovementPolicy`. The FRS calls this "an
unconfirmed business constant", so each run records which set produced its rows
— re-scoping later means re-running the adapter, not reinterpreting stored data.
Other movements in the extract (311 transfers, 641 deliveries, 543
subcontracting) are excluded pending a VZI ruling.

**No business rule is evaluated.** `DISMM` and `MSTAE` are staged as found; no
column stores an OAR verdict, a demand class, or any calculated parameter. A
boundary test asserts this.

## Data quality

Rejections are recorded with a stable reason code, never dropped silently. Per
run, counts are exact; stored rejection rows are capped at 5,000 so a systemic
fault cannot write a million rows.

| Reason | Count | Meaning |
| --- | --- | --- |
| `receipt_before_creation` | 1,171 | GR predates its PO. Line is staged; lead time is not. |

Other codes exist and stayed at zero on this extract: `missing_material`,
`missing_plant`, `invalid_date`, `invalid_quantity`,
`missing_purchasing_document`.

## Coverage, and what it means for Phase 3

| Field | Coverage | Note |
| --- | --- | --- |
| MRP type (DISMM) | 45,360 / 45,409 | 49 blank — *unknown*, not "not OAR" |
| Planned delivery time | 44,495 | The lead-time fallback |
| Description | 5,904 / 5,904 | Complete for MARA's population |
| Unit price | 5,892 | MBEW; no currency column in the extract |
| Consumption | 6,901 material-plants | of 45,409 |
| PO with goods receipt | 22,545 | 21,048 have a usable duration |
| **Criticality** | **788 material-plants (1.7%)** | see blocker below |
| **Current safety stock** | **0** | EISBE absent from the extract |

## Blockers for Phase 3

**Criticality reaches 1.7% of material-plants.** ZMM065 covers 10,929 MARC
materials, but criticality is staged on the *material* record, which comes from
MARA — and MARA covers only 8% of the MARC population. The join therefore yields
788 material-plants. Criticality drives the service level, which drives Z, which
drives safety stock, so 98% of the catalogue cannot reach a Z factor by this
route. Two possible fixes, both business decisions: source criticality at
material-plant grain directly from ZMM065, or obtain a fuller MARA extract.

**EISBE (safety stock) is absent from the MARC extract.** The delivered export
carries 19 columns and safety stock is not among them, though the FRS names
EISBE as required and FR-9 tracks changes to it. `current_safety_stock` is NULL
throughout — not zero, since zero is a real and different claim. A recommendation
can propose a safety stock but has nothing to compare against. Re-extraction
request, not a code fix.

**MBEW has no currency column.** `unit_price` is staged without one. ZAR is
obvious from context but writing it in would be inventing source data.

**Still unresolved from Phase 0, untouched here:** the consumption movement-type
set, the OAR rule confirmation and roll-up, service levels, Max Stock strategy,
and lead-time ownership (calculated vs the I11 Z-program). Phase 2 stages the
inputs all of them need.

## What Phase 3 reads

```python
from app.initiatives.i7.adapters import (
    consumption_series_for,   # dense monthly series -> ADI / CV²
    iter_material_attributes, # streams material-plants -> OAR policy
    material_attributes_for,
    purchase_orders_for,      # unfiltered PO observations -> lead-time stats
)
```

Not the staging tables directly, and never `raw_*`.

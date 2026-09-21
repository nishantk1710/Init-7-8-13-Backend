# Spares AI — Backend

FastAPI backend for Spares AI.

> **Foundation.** I07 / I08 / I13 business logic is intentionally not implemented.
> The shared infrastructure underneath it is: the CPI/SAP client, the Azure SQL
> persistence layer, the Data Lake storage adapter and the AI service layer all
> exist and are wired to VZI's non-prod Azure.

It exists to prove the skeleton: FastAPI runs, routing is organised, the frontend can
reach it, it deploys independently, and I07/I08/I13 have clean extension points.

## Product shape

One backend application, three business modules — **not** three FastAPI services:

| Module | Scope | Future route prefix |
| --- | --- | --- |
| I07 | Predictive Inventory / ROP / Safety Stock | `/api/i7/*` |
| I08 | Refurbishable Spares | `/api/i8/*` |
| I13 | OAR Utilisation Tracking | `/api/i13/*` |

### Architecture rules

I07, I08 and I13 must **not** import each other's internals, and must **not** each build
their own SAP client. Integrations are shared infrastructure:

```
                 Shared SAP adapter
                        │
             ┌──────────┼──────────┐
             │          │          │
            I7         I8         I13
```

The same applies to Azure SQL and authentication. Flow of dependencies:

```
api/ -> services/ -> integrations/ -> external platform
```

Anything common across initiatives belongs in `app/shared/` or `app/services/`.

## Folder structure

```
backend/
├── app/
│   ├── main.py                  FastAPI app factory: CORS, logging, router mount
│   ├── api/
│   │   ├── router.py            central router; initiative routers register here
│   │   ├── health.py            GET /api/health
│   │   ├── events/pr.py         POST /api/events/pr (stub)
│   │   └── i7/  i8/  i13/       route extension points (empty)
│   ├── core/
│   │   ├── config.py            environment-driven settings
│   │   ├── logging.py           stdout logging setup
│   │   └── security.py          auth placeholder (Entra ID / SAP callbacks)
│   ├── integrations/            sap/  azure/  entra/  notifications/  (empty)
│   ├── integrations/sap/        the CPI client — the ONLY SAP access
│   ├── integrations/ai/         LLM adapters — the ONLY provider imports
│   ├── prompts/                 versioned prompt templates
│   ├── seed/                    loads the July extracts into Azure SQL
│   ├── initiatives/             i7/  i8/  i13/  business logic (empty)
│   ├── services/                business services (empty)
│   ├── models/                  persistence models (empty)
│   ├── schemas/                 shared Pydantic schemas (empty)
│   └── shared/                  cross-initiative helpers (empty)
├── alembic/                     migrations (env.py reads DATABASE_URL from settings)
├── alembic.ini                  sqlalchemy.url deliberately blank
├── tests/test_health.py
├── tests/test_db.py
├── tests/test_storage.py        adapter-agnostic conformance suite
├── tests/test_seed.py
├── tests/test_sap.py            client mechanics, on a fake transport
├── tests/test_sap_contract.py   does SAP still look the way we believe?
├── tests/test_ai.py             AI layer, leakage guard, live conformance
├── requirements.txt             runtime + test dependencies
├── pytest.ini
├── .env.example
└── README.md
```

Empty packages are deliberate: they are agreed extension points, so three developers can
add modules in parallel without colliding.

## Requirements

| | Version | Notes |
| --- | --- | --- |
| Python | 3.11+ | developed on 3.12 / 3.13 |
| ODBC Driver 18 for SQL Server | 18 | **not** a pip package; ships with the App Service image, installed by hand locally |
| Azure CLI | any current | for `az login`, so the Data Lake adapter has an identity |

> **This backend no longer runs against anything local.**
> Local Postgres and a local extract folder were stand-ins while VZI's Azure was
> being provisioned. Both are gone. The database is Azure SQL (`sqldb-aicom`)
> and storage is the `stvziaicomnonprod` Data Lake, and a `postgresql://` or
> filesystem URL is now refused with an explanation rather than half-working.
>
> Both sit behind private endpoints with public network access disabled, so
> **neither is reachable from a laptop.** That is the network working as
> designed. What you can do locally is run the test suite — it covers the
> storage adapter against an in-memory fake — and reach CPI and Foundry, which
> are the two dependencies that are not private. Everything else is proven from
> inside the App Service with `python -m app.checkup`.

## Setup, from a fresh clone

Three steps to a running test suite. Seeding and anything database-backed
happen on Azure — see **Deployment**.

### 1. Python environment

```bash
cd backend
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

On Windows you can skip activation entirely and call `.venv\Scripts\python.exe`
directly — it avoids the PowerShell execution-policy prompt.

### 2. Configuration

```bash
cp .env.example .env
```

Everything has a working default except the two Azure resources, and **both are
optional locally** — leave them empty and the app, the liveness endpoint and the
whole test suite still work:

| Variable | Value | Notes |
| --- | --- | --- |
| `DATABASE_URL` | `mssql+pyodbc://<user>:<pw>@sql-vzi-aicom-nonprod-san.database.windows.net:1433/sqldb-aicom?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes` | unreachable outside the VNet |
| `STORAGE_URL` | `abfss://<container>@stvziaicomnonprod.dfs.core.windows.net/<path>` | unreachable outside the VNet |
| `FOUNDRY_API_KEY` | the VZI non-prod key | reachable from anywhere |
| `CPI_*` | four values | reachable from anywhere |

`Encrypt=yes` is not optional — Azure SQL refuses the connection without it.
Do **not** set `TrustServerCertificate=yes` to work around a certificate error:
it connects while trusting anything, which is the same objection as disabling
TLS verification for CPI.

`.env` is gitignored. Never commit real credentials.

### 3. Run the tests

```bash
pytest                                    # 360 pass, 12 fail, 14 skip
uvicorn app.main:app --reload --port 8000
```

**The 12 failures are expected right now, and are not your code.** They are
`drift`-marked: they compare the SAP discovery snapshot against the constants in
`known_conditions.py`, and a discovery re-run on 15-Sep moved the snapshot —
five `ZMM_KPI02_SRV` entity sets now reject every request with HTTP 400. They
stay visible on purpose rather than being re-baselined green, because that would
erase the only signal that ~1.17M rows of change-document data stopped being
readable. See `pytest.ini`, marker `drift`.

To run only what gates a deployment — which is what CI runs, and which is green:

```bash
pytest -m "not live and not drift"        # 356 pass, 14 skip
```

Spell out both markers. A command-line `-m` **replaces** the one in `addopts`
rather than combining with it, so `-m "not drift"` alone silently re-enables the
live SAP tests and starts calling CPI.

```bash
curl http://localhost:8000/api/health     # 200 - liveness, no dependencies
curl http://localhost:8000/api/ready      # 503 locally - see below
```

The 14 skips are the Azure-dependent tests, and `/api/ready` returning 503
locally is **correct**: it touches the database and storage, and neither answers
from outside the VNet. `not_configured` means the variable is empty;
`unavailable` means it is set but the dependency did not answer.

To see exactly which dependency is unhappy, and why:

```bash
python -m app.checkup
```

That reports the database, storage, CPI and the model separately, prints no
secret, and is the same command used to satisfy **W2.2** from inside the App
Service. Locally, expect CPI and the model green and the other two failing.

### Seeding the real SAP data

The seed reads from the Data Lake and writes to Azure SQL, so it runs **inside
the App Service**, not here. The `STORAGE_URL` path must contain both delivery
folders:

```
<STORAGE_URL>/
├── KPI 02 Data Extract/Tables/      24 SAP table extracts
└── Resources Shared - Rohit/        ZMM065 x2 + 30 Day GR Report
```

```bash
python -m app.seed --list    # checks every file is present; touches no database
python -m app.seed --all     # ~30 minutes, 3.4M rows
```

`--list` marks anything it cannot find as `[MISSING]`, and `--all` runs the same
check before touching a table — so a wrong `STORAGE_URL` costs a message, not a
half-loaded database.

See [Seeding](#seeding) for the layout rules and what the data does and does not
cover.

## Configuration

Settings come from environment variables, falling back to a local `.env`:

```bash
cp .env.example .env
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_ENV` | `local` | Environment name |
| `APP_NAME` | `spares-ai-backend` | Service identity + Swagger title |
| `APP_VERSION` | `0.1.0` | API version |
| `API_PREFIX` | `/api` | Prefix for every route |
| `LOG_LEVEL` | `INFO` | Root log level |
| `FRONTEND_ORIGIN` | `http://localhost:3000` | Allowed CORS origin(s), comma-separated |

Reserved and **not required for startup** — every one may be empty:
`DATABASE_URL`, `STORAGE_URL`, `CPI_*`, `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`.

`.env` is gitignored. Never commit real credentials.

## Endpoints

### `GET /`

Service index. Returns what this service is and where to go next, so the bare host does
not answer with FastAPI's default `{"detail":"Not Found"}`.

```json
{
  "service": "spares-ai-backend",
  "version": "0.1.0",
  "status": "ok",
  "message": "spares-ai-backend is running. See docs_url for the API.",
  "docs_url": "/docs",
  "health_url": "/api/health"
}
```

→ `200 OK`. Use `/api/health`, not this, for uptime probes.

### `GET /api/health`

Liveness only — no database, SAP, Azure, LLM or auth dependency. Works with zero
credentials configured.

```bash
curl http://localhost:8000/api/health
```

```json
{ "status": "ok", "service": "spares-ai-backend" }
```

→ `200 OK`

### `POST /api/events/pr`

SAP-facing connectivity stub. Accepts arbitrary JSON, logs that an event arrived, and
returns `202`. It does **not** persist, classify, call SAP, run workflows, or route into
I07/I08/I13. The payload is intentionally untyped — the SAP contract is not confirmed.

```bash
curl -i -X POST http://localhost:8000/api/events/pr \
  -H "Content-Type: application/json" \
  -d '{"prNumber":"10012345","plant":"1300"}'
```

```json
{ "status": "received" }
```

→ `202 Accepted`, plus a log line:

```
INFO app.api.events.pr: PR event received (fields=2, keys=['plant', 'prNumber'])
```

Only the payload's *shape* is logged at INFO. Contents go to DEBUG only, since the SAP
body may carry sensitive fields once the real contract lands. Never log credentials,
tokens or authorization headers.

## Authentication

Not implemented — see [`app/core/security.py`](app/core/security.py). Application users
are expected to authenticate via Azure Entra ID. The inbound SAP mechanism (OAuth, API
key, mTLS, or private-network trust) is **not decided** and must not be assumed until
the SAP/VZI team confirms it. The PR stub is therefore deliberately unauthenticated.

## Tests

```bash
pip install -r requirements.txt

pytest                                # everything except live SAP
pytest -m "not live and not drift"    # the deployment gate; CI runs this
pytest -m live                        # live SAP + live Foundry; needs credentials
pytest -m drift                       # SAP contract drift only
```

Three markers, and the distinction between them is the point:

| | What it proves | Blocks a deploy? |
| --- | --- | --- |
| unmarked | our code behaves | **yes** |
| `drift` | SAP still matches what we recorded | no — reported only |
| `live` | the real CPI and Foundry answer | no — excluded by default |

`drift` and `live` are excluded from the gate because they fail for reasons that
are not our code: SAP changing underneath us, or a machine with no credentials.
Leaving them in would make red the normal state, which is how a suite stops
being read.

**A command-line `-m` replaces the one in `addopts`, it does not combine.** So
`-m "not drift"` silently re-enables the live tests and starts calling CPI —
which is why the gate spells out `"not live and not drift"`.

Azure-dependent tests skip with a reason naming the network cause: `sqldb-aicom`
and `stvziaicomnonprod` are behind private endpoints and answer only from inside
the VNet. The storage adapter is still covered — against `tests/fake_adls.py`,
an in-memory stand-in for the Azure SDK.

## Database

**Azure SQL — `sqldb-aicom` on `sql-vzi-aicom-nonprod-san`.** `DATABASE_URL` is
the only place it is named.

Local Postgres was a stand-in while this was being provisioned and has been
removed. `app/core/db.py` now refuses any other backend outright rather than
letting SQLAlchemy fail deeper in with a driver error, because the real problem
— that there is no longer a local database — is not obvious from that.

```bash
alembic upgrade head        # from inside the App Service; see Deployment
```

Two things about this database that are easy to lose an afternoon to:

- **It is unreachable outside the VNet.** Public network access is disabled and
  it is fronted by `pe-sqlvziaicomnonprod-sqlserver`. `DATABASE_CONNECT_TIMEOUT_SECONDS`
  defaults to 5 so that presents as a fast, clear failure instead of a hang.
- **`pyodbc` needs a system driver.** "ODBC Driver 18 for SQL Server" is not a
  pip package. It is present in the App Service Python image; a local machine
  has to install it separately.

| Endpoint | Touches dependencies | Purpose |
| --- | --- | --- |
| `GET /api/health` | no | liveness — must work with nothing configured |
| `GET /api/ready` | database + storage + AI | readiness — `200` only when both are `ok` |

`/api/ready` reports each dependency as `ok`, `not_configured` or `unavailable`,
and checks both even when the first has already failed, so one request tells you
everything that is wrong:

```json
{"status": "ready",     "database": "ok",             "storage": "ok",             "ai": "ok"}
{"status": "not_ready", "database": "ok",             "storage": "unavailable",    "ai": "ok"}
{"status": "not_ready", "database": "not_configured", "storage": "not_configured", "ai": "ok"}
```

Keep these separate. A platform probe pointed at a health check that touches the
database will restart a healthy container over a database blip.

### Rules for models

- **Portable SQLAlchemy constructs only** — no `JSONB`, `ARRAY` or `ON CONFLICT`.
  The move off Postgres proved the point: the ORM models needed no change at all,
  while the one place that reached past SQLAlchemy to the driver — the `COPY`
  bulk load — had to be rewritten. That is now `app/seed/sqlserver.py`, and it
  is the only file in the codebase that knows which database this is.
- **Import every model in `app/models/__init__.py`.** Alembic autogenerate diffs
  against `Base.metadata`; a model nothing imports silently never gets a migration.
- **Raw SAP landing tables belong in `app/models/`**, the shared foundation — not
  inside an initiative package. Three initiatives read the same extracts, and
  three private copies of an MSEG table diverge within a week. Tables *derived*
  from those belong to the initiative that derives them.

### Migrations

```bash
alembic revision --autogenerate -m "what changed"   # review the file before committing
alembic upgrade head
alembic downgrade -1
```

`alembic.ini`'s `sqlalchemy.url` is blank on purpose — `alembic/env.py` supplies
it from settings. Do not restore it: a URL there is a credential in a tracked file.

## Storage

**Azure Data Lake Gen2 — the `stvziaicomnonprod` account**, reached through
`pe-stvziaicomnonprod-dfs`. `STORAGE_URL` is the only place it is named.

```bash
STORAGE_URL=abfss://<container>@stvziaicomnonprod.dfs.core.windows.net/<path>
```

The local-folder adapter was a stand-in and has been removed, so a filesystem
path is refused — with a message that says what to set instead, because
`urlparse` reads `C:/data` as scheme `c` and "unsupported scheme 'c'" helps
nobody.

**Authentication is by managed identity, not a key.** `DefaultAzureCredential`
resolves to the App Service's identity when deployed and to your `az login`
session locally, so no storage secret is stored or rotated anywhere. The
identity needs **Storage Blob Data Reader** on the account; without it every
read is a 403 that reads like a missing file. `AZURE_STORAGE_ACCOUNT_KEY` exists
as a fallback for a machine with neither, and should stay unused.

**The extracts must be uploaded to the container.** They are ~800 MB and are not
in this repository, and the seed no longer reads them from a local folder.

### Using it

```python
from app.core.storage import get_storage, sha256_of

storage = get_storage()
for key in storage.list("extracts"):
    with storage.open_read(key) as handle:      # streamed, never materialised
        ...
```

Reads and writes are **streaming file objects**, not `bytes`. Hashing the 56 MB
`CDHDR1.XLSX` peaks at 2 MB resident; a `read() -> bytes` interface would have
peaked at 56 MB locally and downloaded the whole blob from cloud storage before
parsing a row. `openpyxl` accepts these handles directly.

Writes are **all-or-nothing** — the payload is buffered and only uploaded when
the `with` block completes — so an interrupted run never leaves a truncated
object that `exists()` then reports as present, and never clobbers good content
with a half-written replacement.

Reads are **seekable**, which is not incidental. An `.xlsx` is a zip, and
`openpyxl` seeks to the central directory before parsing a row; Azure's
`StorageStreamDownloader` is forward-only. Every read therefore lands in a
`SpooledTemporaryFile` — in memory up to 32 MB, spilled to disk beyond — so a
143 MB workbook still costs flat memory. An adapter that handed the downloader
back directly would pass every behavioural test and fail on the first real
workbook.

### Keys

Keys are strings with forward slashes: `extracts/MSEG_1.XLSX`. Never `Path`
objects, never backslashes, never absolute. A `Path` leaking through the
interface works on Windows and then silently creates wrongly-named blobs on
Azure — so `..`, backslashes, leading slashes and empty segments are all
refused rather than resolved.

### Adding an adapter

1. Implement `Storage` in `app/integrations/storage/`.
2. Register its scheme in `app.core.storage.build_storage`.
3. Add it to the `storage` fixture's `params` in `tests/test_storage.py`.

Step 3 is the point: the conformance suite is written against the interface, so
a new adapter inherits ~20 behavioural tests without a line of new test code.
That is how the Data Lake adapter was validated — it runs against
`tests/fake_adls.py`, an in-memory stand-in for the Azure SDK, and the fake
caught three real bugs on its first run.

Be clear about what that proves and what it does not. The fake exercises
behaviour: failed writes, sorted recursive listings, missing-key errors. It
cannot exercise RBAC, DNS, private endpoints or throughput. Those are proven by
`python -m app.checkup` from inside the App Service, and by nothing else.

## Seeding

Loads the July SAP extracts into the local database. This replaces
`data-generator/` as the development data source -- no synthetic data.

```bash
python -m app.seed --list          # manifest + whether each source file is present
python -m app.seed --all           # load everything
python -m app.seed --table marc    # one table
python -m app.seed --all --force   # reload even if nothing changed
```

Needs `DATABASE_URL` and `STORAGE_URL`. `--list` touches no database.

### Two layers, and why

```
XLSX extract  ->  raw_<table>   mirrors the extract exactly, every column, text
                      |
                 normalise      translation map + MATNR padding  (NOT YET BUILT)
                      v
                  <table>       matches the OData contract; what initiatives read
                      ^
CPI live pull  -------+         same shape, different loader
```

Only the raw layer exists today. It is deliberately a faithful copy, because the
extract and the OData projection are **not the same data**:

| | Extract | OData |
| --- | --- | --- |
| MARA columns | 244 | 7 |
| Column names | `Ext. Material Group` | `Extwg` (not exposed) |
| Material number | `2000000131` | `000000008000000000` |

So nothing joins the two automatically. The normalise step -- a reviewed
business-label to SAP-field to OData-property map, plus MATNR zero-padding -- is
the remaining work, and it is the layer initiatives should read. Building
directly on `raw_*` means rewriting when the loader is swapped for a live pull.

Worth knowing: the extract carries `Ext. Material Group` (EXTWG), the field the
I07 and I13 FRSs both mark **BLOCKING** because CPI does not expose it. Its
population is not yet measured, but the column is there.

### Everything is text

Every raw column is `text`. SAP keys are digit strings, and any numeric coercion
at load time turns `000000008000000000` into `8e+15` irreversibly. Typing belongs
in the normalise step, where it is reviewable.

### Re-runs

Each file's SHA-256 is recorded in `ingestion_run`. A second run whose sources
are unchanged is skipped; `--force` overrides. Each table loads in one
transaction -- drop, create, copy, record, commit -- so a failed table rolls back
to its previous contents and the other 27 still load.

### Two deliveries, one storage root

`STORAGE_URL` points at the parent `Vedanta` folder and every key carries its
delivery prefix — the same shape a cloud container has:

```
KPI 02 Data Extract/Tables/   24 SAP table extracts
Resources Shared - Rohit/     ZMM065 (both plants) + the 30-day GR report
```

The reports are not table dumps. They are multi-sheet workbooks with title rows
and pivots, so their manifest entries name a `sheet` and a `header_row`; the SAP
extracts use the defaults (first sheet, row 1). Reading BMM's ZMM065 with the
defaults would take a pivot table's first row as the header and produce a table
of `column_1`, `column_2` labels — silently, which is why the override exists.

`raw_zmm065_bmm` / `raw_zmm065_gb` also carry the **criticality tiers**
(CRITICAL / IMPACT / INSURANCE / NORMAL / OBSOLETE) that the FRSs call the
platform-side critical parts list (D3 interim). All three initiatives need them.

### Handing this to someone else

Only two things are machine-specific, and both live in `.env` (never committed):

```
DATABASE_URL=mssql+pyodbc://<user>:<pw>@sql-vzi-aicom-nonprod-san.database.windows.net:1433/sqldb-aicom?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes
STORAGE_URL=abfss://<container>@stvziaicomnonprod.dfs.core.windows.net/<path CONTAINING both delivery folders>
```

The **folder layout must match**, because the manifest keys carry the delivery
prefix:

```
<STORAGE_URL>/
├── KPI 02 Data Extract/Tables/      24 SAP table extracts
└── Resources Shared - Rohit/        ZMM065 x2 + 30 Day GR Report
```

`STORAGE_URL` points at the parent, not at either sub-folder. Anything else in
that tree is ignored — the loader reads only the 27 files the manifest names.

Run `python -m app.seed --list` first. It touches no database and marks any file
it cannot find as `[MISSING]`. `--all` and `--table` preflight the same check and
refuse to start if anything is absent, so a wrong `STORAGE_URL` costs a message
rather than half a loaded database.

Two portability notes: file names are matched **exactly**, so on a
case-sensitive filesystem (CI, Linux, macOS) `Mara.XLSX` and `MSEG_2.XLSX` must
keep their delivered casing. And the reports' sheet names (`Sheet1`, `Sheet2`,
`GR REPORT`) are what SAP exported — a re-export could rename them, and the
loader will say so by name rather than loading the wrong sheet.

### Plant scope, and the coverage gap to know about

**The scope is two plants: 1300 (Black Mountain Mining) and 1500 (Gamsberg).**
The list lives once, in `app/shared/plant_scope.py`, and is enforced on read in
two places — the I08 views (through a generated `in_scope_plant()` SQL
function) and the I13 `postgres_*` adapters. The `raw_*` tables stay a faithful
copy of the July spreadsheets, other plant codes and all; nothing is deleted,
it is filtered when read. A query for an out-of-scope plant returns nothing.

Within that scope, two tables cover Black Mountain only while everything around
them covers both sites. That asymmetry is what makes the gap easy to miss:

| Table | In-scope coverage |
| --- | --- |
| `raw_marc` | 1300 only (45,352) — **Gamsberg 1500: zero rows** |
| `raw_eban` | 1300 only (234,310) — **Gamsberg 1500: zero rows** |
| `raw_mard` | 1300 and 1500 |
| `raw_resb` | 1300 and 1500 |
| `raw_ekpo` | 1300 and 1500 (1500: 35,083) |

MARC is the consequential one: it holds MRP type, reorder point and planned
delivery time. **Every DISMM / OAR statistic derived from this data is a Black
Mountain figure, not a business-wide one**, and I07 cannot recommend parameters
for Gamsberg at all until MARC covers it. Stock and movements *look* complete,
which is exactly the trap.

Both are re-extraction requests, not code fixes.

## SAP access

Read-only, through the CPI generic OData consumption endpoint. `SapClient` is
the only thing application code should import:

```python
from app.integrations.sap import SapClient

client = SapClient()
page = client.read("MaterialPlantSet", filter="Dismm eq 'ND'", top=100)
all_rows = client.read_all("MaterialPlantSet")     # ordered, paged, complete
```

Callers name an entity set. Which service owns it, the iFlow URL, the token, the
OData envelope, typed decoding and paging are all below that line. **Nothing here
writes** — P1 forbids SAP write-back programme-wide, and only GET is issued.

### Three behaviours that are measured, not assumed

**Paging is always ordered.** `discovery/paging_stability.txt` recorded an
unordered full pull of `MaterialPlantSet` returning 2,178 rows but only 1,618
distinct keys — 560 duplicated, 560 missing, and a *different* 560 next time.
`read_all` refuses to page without `$orderby` rather than offering it as an option.

**`$count` is not trusted.** It returns HTTP 500 on some sets. `read_all` asks
once and, on failure, pages until a short page arrives. The demotion is reported
in `ExtractResult.counted`, not hidden.

**Filters that SAP silently ignores are refused.** `discovery/filter_support.csv`
probed 208 filterable properties: 124 honoured, 11 rejected with HTTP 500, and
**62 silently ignored** — SAP drops the filter and answers HTTP 200 with the whole
set. There is no error to catch, so the guard runs before the request:

```python
client.read("PurchaseOrderItemSet", filter="Pstyp eq '3'")
# UnsupportedFilterError: SAP SILENTLY IGNORES a filter on Pstyp ...
```

That is the exact case behind "apply the Pstyp filter on the EKPO pull, not in
the query". Read the set without the predicate and filter client-side, or pass
`allow_unsupported_filter=True` if you have re-verified it against live SAP.

### Errors

| Class | Meaning | Retry? |
| --- | --- | --- |
| `AuthError` | credentials or token exchange failed | no |
| `TransientError` | 5xx or network fault | yes, with backoff |
| `NotFoundError` | 404 — wrong path | never |
| `RequestError` | other 4xx, usually a bad filter | no |
| `ContractError` | response contradicts the contract — drift | no |
| `UnsupportedFilterError` | raised *before* the call, see above | n/a |

### Testing

Almost every test uses an injected transport and needs no network. The tests that
call live SAP are marked `live` and **excluded from the default run and from CI**
(see `pytest.ini`), because they fail for reasons that are not the code — no
credentials, no network, or a proxy terminating TLS.

```bash
pytest              # 289 tests, no network
pytest -m live      # 8 tests, from a machine that can reach CPI
```

If `pytest -m live` fails with `CERTIFICATE_VERIFY_FAILED`, the network is
intercepting TLS: set `CPI_CA_BUNDLE` to the corporate root certificate. Do not
reach for a way to disable verification — there deliberately isn't one.

### Contract testing — is SAP still what we think it is?

Three layers, and the distinction is the point.

| Layer | Runs | Proves |
| --- | --- | --- |
| Snapshot consistency | always | the CSVs and the committed `$metadata` agree |
| Known conditions | always | the snapshot still says what we reasoned about |
| Live drift | `-m live` | SAP still matches the snapshot **today** |

The first two prove we are *self-consistent*; only the third proves we are
*right*. Conflating them would be dishonest, so they are separate classes in
`tests/test_sap_contract.py`.

Layer 1 is not theoretical. A discovery folder was once replaced with a sweep
from a day earlier; the CSVs and the XML disagreed and nothing noticed until
something downstream broke. That comparison now runs on every test run:

```python
compare_contracts(contract(), parse_snapshot())   # must be []
```

Layer 2 lives in `known_conditions.py` — every constant an observation made
against live CPI. **When one of those tests fails, the first question is "did SAP
change?", not "is the expectation wrong?"** Particularly for `DISMM`: its value
set decides OAR scope, so quietly adding a newly-seen MRP type to make a test
green would silently change which materials three initiatives act on.

### Live verification, 2026-09-11

Run from a machine without TLS interception. **3 of 7 live tests passed, 4
failed, and the failures were findings rather than defects.**

Confirmed working against real SAP:

- OAuth token acquisition, the CPI iFlow envelope, and typed decoding
  (`rows: 5 | unknown properties: []`)
- **Zero metadata drift on both services** — no key, type or property has moved
- `Dismm` filter still honoured, so OAR material scope is safe
- `$count` stable across repeated calls

Four findings, in priority order:

**1. `MaterialPlantSet` returns HTTP 500 for ANY two-field `$orderby`.**
`Matnr,Werks` and `Werks,Matnr` both fail; every single-field ordering works;
`Ebeln,Ebelp` works fine on `PurchaseOrderItemSet`. The body is empty, which
means a backend short dump rather than a rejected query. This blocked
`read_all()` entirely — it died on page 1.

The client now negotiates: it tries the full key, and on a server error falls
back to the longest prefix SAP accepts, reporting `order_by_degraded`. It also
**measures** `duplicate_keys` afterwards, because a shortened ordering is not
unique (`Matnr` alone repeats across plants) and duplicates in the output are
direct evidence of rows missing from it. Still worth raising with SAP Basis:
an empty-bodied 500 should have left an ST22 short dump.

**2. `Pstyp` is no longer silently ignored — it now returns HTTP 500.** The
opposite direction from what the snapshot records. Normal callers are unaffected
(the filter guard already refuses both `IGNORED` and `REJECTED_HTTP_500`), but
anything passing `allow_unsupported_filter=True` will now fail after exhausting
its retries. `filter_support.csv` needs a discovery re-run.

**3. `MaterialValuationSet` answers HTTP 400 to everything**, not just `$count`
— a plain `$top=5` fails too. Recorded in `known_conditions.UNREADABLE_SETS`.
No data impact: MBEW comes from the July extract (`raw_mbew`, 7,034 rows).

**4. `MaterialPlantSet` grew 2,178 → 2,183.** Normal growth. The test asserted
exact equality and has been changed to a tolerance — a test that cannot
distinguish five new rows from a step change trains people to ignore it.

Worth recording: the old 2,178 → 1,618 row-loss did **not** reproduce on the
day. That is not evidence ordering is unnecessary — sort stability is data- and
load-dependent — but it does mean no rows are being lost right now.

### Why this is Python and not TypeScript

The frontend used to carry a TypeScript CPI client at `frontend/src/lib/sap/`.
It was removed on 2026-09-11. Two implementations of one wire protocol have to
be fixed twice on every SAP change, and SAP changed twice in a week during
development alone — `Edm.Decimal` to `Edm.String` on two properties, and a
requisition key gaining a field.

What moved here: the entity-set contract, EDMX parsing, drift comparison,
paging, and the per-set mock/live routing. What stayed in the frontend:
`src/lib/material-scope/`, the OAR rule — a business decision rather than a wire
protocol, and the thing `routeMaterial()` consumes.

## AI service layer

Four features need a language model — I07's recommendation rationale, I08's
free-text screening, I13's quantity-suggestion reason, and the reservation
assistant. If each called a provider directly, changing provider would mean
changing four places and finding the fourth in production.

So this is a socket. Business logic asks for a completion; it never names a
provider, endpoint, deployment or model:

```python
from app.core.ai import get_llm, Message
from app.core.prompts import get_prompt

prompt = get_prompt("i07_recommendation_rationale")
text = prompt.render(material="500-14892", plant="1300", ...)
answer = get_llm().complete([Message("user", text)])
```

Which provider is plugged in comes from one setting:

| `LLM_PROVIDER` | Provider |
| --- | --- |
| `stub` *(default)* | Deterministic, no network, no credentials |
| `foundry` | Microsoft Foundry |
| `openai` | Any OpenAI-compatible endpoint |

**The stub is not a placeholder.** It means the application, its tests and a
developer laptop all work with no provider at all — the same property the
database and storage ports have. Only the endpoint and key wait for Azure.

### The rule, and what enforces it

W1.5's deliverable is *"business logic never imports a provider SDK directly"*.
That is a rule until something checks it, so `tests/test_ai.py` walks every
module under `app/` and fails the build if a provider package is imported
outside `app/integrations/ai/`. The fix when it fails is to call `get_llm()` —
never to widen the allow-list.

### Why there are two HTTP adapters

An abstraction with one implementation is untested: you only discover you have
baked in provider-specific assumptions when you try to plug in something else.
So the alternate differs from Foundry in every place that matters, and tests
assert that it does:

| | Foundry | Alternate |
| --- | --- | --- |
| URL | `…/deployments/{deployment}/chat/completions?api-version=…` | `…/chat/completions` |
| Auth | `api-key:` header | `Authorization: Bearer` |
| Model named in | the URL | the body |

Both inherit retry, timeout and token logging from `http_base.py`, so a third
adapter is about forty lines.

### Prompts are files, and that matters for audit

Prompts live under `app/prompts/<id>/vN.md` — one directory per prompt, one file
per version, highest version winning unless one is pinned. Markdown because
these are paragraphs of English that people review.

The reason is provenance, not convenience. Every I07 recommendation carries an
LLM-written rationale that a person reads before approving a stock change. When
someone asks six months later why it said what it said, *"the model wrote it"* is
not an answer — so `Completion` carries `prompt_id` and `prompt_version` through
to the caller.

Rendering refuses to guess: a missing **or** unexpected placeholder raises rather
than sending a prompt containing a literal `{material}` to the model and getting
confident nonsense back.

### The forecast port is deliberately thin

`Forecaster` exists alongside `LLMProvider`, with one in-process Croston
implementation. It is **not** I07's forecasting engine — that is W4.2, which is
in-process statistics with no provider to abstract and does not depend on this
module. The port exists so call sites survive forecasting later moving to a
hosted endpoint. Growing it further today would be speculative.

### Foundry: two endpoint shapes, and why it matters

Confirmed 15-Sep: VZI runs **GPT-4o and gpt-4o-mini** on

```
https://oai-vzi-aicom-nonprod-san.services.ai.azure.com/openai/v1
```

That trailing `/openai/v1` is not cosmetic. Foundry exposes two request shapes:

| | **v1** (this endpoint) | **deployments** (classic Azure OpenAI) |
| --- | --- | --- |
| URL | `{endpoint}/chat/completions` | `{endpoint}/openai/deployments/{name}/chat/completions?api-version=...` |
| Model named in | the body | the URL |
| Auth | `Authorization: Bearer` | `api-key:` |
| `api-version` | not used | required |

Appending the classic path to a `/openai/v1` endpoint yields a doubled
`/openai/` and a 404 that reads like a permissions problem — an afternoon lost
to the wrong diagnosis. `FOUNDRY_API_STYLE=auto` infers the shape from the
endpoint; both are implemented and tested, and the override exists for an
endpoint that breaks convention.

### Checking it end to end

```bash
python -m app.integrations.ai
```

Prints the resolved configuration, the inferred API style, the **exact URL**
being called, the model routing, then calls each deployment once and reports
tokens and latency. It never prints the key, so the output is safe to paste into
a status update or send to whoever configured the deployment.

Reading the failure matters: a **401** means the URL is right and the key is
wrong; a **404** means the URL is wrong. Those need different fixes, and this
makes which one obvious.

### Which model each job uses

Two deployments differing in cost, not correctness — so the choice lives in
`app/core/model_registry.py` rather than at the call site:

| Task | Tier | Why |
| --- | --- | --- |
| I07 rationale | fast | Short, formulaic, high volume; the reader checks the numbers |
| I08 coding-candidate | capable | Language judgement over 5,225 messy free-text lines; a false negative is a repairable bought new |
| I13 quantity suggestion | fast | Explains an arithmetic result |
| Reservation assistant | capable | Interactive, and shapes a purchasing decision |

Routes are written as an *intent* (`fast` / `capable`) rather than a deployment
name, so a renamed deployment moves one setting instead of four call sites. With
only `FOUNDRY_DEPLOYMENT` set, everything falls back to it — degrading to the
costlier model rather than failing.

## Logging

Console/stdout only, configured centrally in `app/core/logging.py` using the standard
`logging` module (never `print()`). Azure App Service captures stdout, so no SDK is
needed; Application Insights can later be attached as an extra handler in that one file
without touching any call site.

## Deployment

Target: **`app-vzi-aicom-nonprod-san`** on `plan-vzi-aicom-nonprod-san`, South
Africa North.

### One-time setup on the App Service

| Setting | Value |
| --- | --- |
| Startup Command | `bash /home/site/wwwroot/startup.sh` |
| Identity | System-assigned, **on** |
| Role assignment | that identity → **Storage Blob Data Reader** on `stvziaicomnonprod` |
| VNet integration | `vnet-vzi-aicom-nonprod-san` / `snet-appservice` |
| `SCM_DO_BUILD_DURING_DEPLOYMENT` | `true` |

The startup command matters more than it looks. Without it Oryx guesses, and its
guess for a FastAPI app is gunicorn's **sync** worker, which cannot run ASGI and
fails with an opaque worker error rather than saying so.

Then the app settings — `DATABASE_URL`, `STORAGE_URL`, `CPI_*`, `FOUNDRY_*`.
Set them on the App Service, never in this repository.

**`DATABASE_URL` does not have to carry a password.** Azure SQL accepts the App
Service's managed identity, which removes the credential entirely:

```
mssql+pyodbc://@sql-vzi-aicom-nonprod-san.database.windows.net:1433/sqldb-aicom
  ?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&Authentication=ActiveDirectoryMsi
```

No code change — `Authentication` is passed straight through to the ODBC driver.
It needs one statement run against `sqldb-aicom` by an Entra admin first:

```sql
CREATE USER [app-vzi-aicom-nonprod-san] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [app-vzi-aicom-nonprod-san];
ALTER ROLE db_datawriter ADD MEMBER [app-vzi-aicom-nonprod-san];
ALTER ROLE db_ddladmin  ADD MEMBER [app-vzi-aicom-nonprod-san];   -- migrations + seed
```

Use a SQL login instead if that admin is not available today; it is the faster
start and the weaker end state. For any setting that does carry a secret, prefer
a Key Vault reference:

```
DATABASE_URL = @Microsoft.KeyVault(SecretUri=https://kv-vzi-aicom-nonprod.vault.azure.net/secrets/database-url/)
```

App Service resolves those into ordinary environment variables before the
process starts, so `Settings` reads them unchanged. **This is why there is no
Key Vault SDK in this codebase** — adding one would buy nothing except another
dependency and another failure mode.

### Deploying

`.github/workflows/deploy.yml` runs the tests, deploys, then curls
`/api/health` and `/api/ready`. It needs `AZURE_WEBAPP_PUBLISH_PROFILE` as a
repository secret, or OIDC via `azure/login`, which is the better long-term
choice — no long-lived credential in GitHub.

Note what the pipeline deliberately does **not** do: run migrations. The GitHub
runner has no network path to `sql-vzi-aicom-nonprod-san`, so `alembic upgrade
head` there cannot work regardless of secrets. It runs from `startup.sh`
instead, on the other side of the private endpoint, and is **off by default**:

```bash
# either set the app setting
RUN_MIGRATIONS_ON_STARTUP=true

# or, from the App Service SSH console
cd /home/site/wwwroot && python -m alembic upgrade head
```

Off by default because nothing serialises it across instances — scale past one
instance with it enabled and two workers can run the same migration at once.

### Proving it works

From the App Service SSH console:

```bash
cd /home/site/wwwroot
python -m app.checkup
```

That checks the database, storage, CPI and the model separately and prints no
secret. **Green there is W2.2** — a live pull from inside the VZI environment,
which is the acceptance criterion and the one thing a laptop cannot demonstrate.

Then seed, which also has to run from here because it reads the Data Lake and
writes Azure SQL:

```bash
python -m app.seed --list        # verifies all 27 files are in the container
python -m app.seed --all         # ~3.4 million rows
```

### The outbound trap

`snet-appservice` is a private subnet with **no default outbound access**. Today
that is harmless: with `vnetRouteAllEnabled` off, internet-bound traffic leaves
via the App Service's own outbound IPs and only private traffic enters the VNet.

Turn route-all **on** — which is the usual companion to a private-endpoint
posture, and someone will propose it — and all egress goes through that subnet.
With no NAT Gateway attached, **CPI and Foundry both stop working**, because
neither has a private endpoint. Nothing in this codebase changes; it simply
stops being able to reach two of its four dependencies.

If that change is made, attach a NAT Gateway and confirm CPI's allowlist covers
its public IP.

## Extending this template

1. Business logic → `app/initiatives/i7|i8|i13/`, orchestration → `app/services/`.
2. Routes → `app/api/i7|i8|i13/`.
3. Register the router in `app/api/router.py` (commented examples are there).
4. External systems → `app/integrations/<platform>/`, one shared adapter per platform.
5. Keep the boundary: no cross-initiative internal imports, no per-initiative SAP client.

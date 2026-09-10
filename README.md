# Spares AI — Backend

FastAPI backend for Spares AI.

> **Foundation only.** I07 / I08 / I13 business logic and all external integrations
> (SAP, Azure SQL, Entra ID, notifications, LLM) are intentionally not implemented.

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
│   ├── initiatives/             i7/  i8/  i13/  business logic (empty)
│   ├── services/                business services (empty)
│   ├── models/                  persistence models (empty)
│   ├── schemas/                 shared Pydantic schemas (empty)
│   └── shared/                  cross-initiative helpers (empty)
├── tests/test_health.py
├── requirements.txt             runtime dependencies
├── requirements-dev.txt         + pytest / test client
├── pytest.ini
├── .env.example
└── README.md
```

Empty packages are deliberate: they are agreed extension points, so three developers can
add modules in parallel without colliding.

## Requirements

Python **3.11+** (developed and verified on 3.13).

## Setup and run

```bash
cd backend
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt          # add -r requirements-dev.txt to run tests
uvicorn app.main:app --reload --port 8000
```

| What | URL |
| --- | --- |
| Service index | http://localhost:8000/ |
| Swagger UI | http://localhost:8000/docs |
| OpenAPI schema | http://localhost:8000/openapi.json |
| Health check | http://localhost:8000/api/health |

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
`AZURE_SQL_CONNECTION_STRING`, `SAP_BASE_URL`, `SAP_CLIENT_ID`, `SAP_CLIENT_SECRET`,
`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`.

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
  -d '{"prNumber":"10012345","plant":"1101"}'
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
pip install -r requirements-dev.txt
pytest
```

Covers `GET /api/health` → 200 / `status == "ok"` and `POST /api/events/pr` → 202.

## Logging

Console/stdout only, configured centrally in `app/core/logging.py` using the standard
`logging` module (never `print()`). Azure App Service captures stdout, so no SDK is
needed; Application Insights can later be attached as an extra handler in that one file
without touching any call site.

## Azure App Service readiness

Deployable as-is; no infrastructure is defined or assumed here:

- binds `0.0.0.0`, port supplied by the runtime
- all configuration from environment variables
- no local-machine-specific paths
- logs to stdout
- health endpoint for probes
- explicit, pinned dependencies

Expected startup command:

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

## Extending this template

1. Business logic → `app/initiatives/i7|i8|i13/`, orchestration → `app/services/`.
2. Routes → `app/api/i7|i8|i13/`.
3. Register the router in `app/api/router.py` (commented examples are there).
4. External systems → `app/integrations/<platform>/`, one shared adapter per platform.
5. Keep the boundary: no cross-initiative internal imports, no per-initiative SAP client.

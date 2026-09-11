# Local development

Frontend and backend are independent processes. Start each in its own terminal.

## Backend — http://localhost:8000

```bash
cd backend
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

Verify:

```bash
curl http://localhost:8000/api/health
# {"status":"ok","service":"spares-ai-backend"}

curl -i -X POST http://localhost:8000/api/events/pr \
  -H "Content-Type: application/json" \
  -d '{"prNumber":"10012345"}'
# HTTP/1.1 202 Accepted -> {"status":"received"}
```

Service index: http://localhost:8000/ · Swagger UI: http://localhost:8000/docs · Tests: `pytest` from `backend/`.

## Frontend — http://localhost:3000

```bash
cd frontend
npm install
npm run dev
```

The UI runs entirely on its own mock data and does not call the backend. Nothing has
been migrated, and the backend does not need to be running to develop the frontend.

## Connecting the two

```bash
cd frontend
cp .env.example .env.local
```

```
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000/api
```

`.env.local` is gitignored. Next.js inlines `NEXT_PUBLIC_*` at build time, so restart
the dev server after changing it.

Call the backend through the shared client — never hardcode `localhost` in a component:

```ts
import { getHealth, apiFetch } from "@/lib/api/client";

const health = await getHealth();
```

## CORS

The backend only accepts browser origins listed in `FRONTEND_ORIGIN`, which defaults to
`http://localhost:3000`. If the frontend runs on another port, set `FRONTEND_ORIGIN` in
`backend/.env` to match. The wildcard `*` is deliberately not used.

A request from an unlisted origin gets no `Access-Control-Allow-Origin` header and the
browser blocks it — if frontend calls fail with a CORS error, check this first.

## Ports in use

| Port | Process |
| --- | --- |
| 3000 | Next.js dev server |
| 8000 | FastAPI / uvicorn |

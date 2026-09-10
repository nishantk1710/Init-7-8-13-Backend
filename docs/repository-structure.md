# Repository structure

One Git repository, one `.git/` at the root, two independently deployable applications.

```
spares-ai/
├── .git/                  the only Git repository
├── frontend/              Next.js + React + TypeScript
├── backend/               FastAPI + Python
├── docs/
├── .claude/launch.json    editor run config (cwd -> frontend/)
├── AGENTS.md  CLAUDE.md   agent instructions (repo-level)
├── .gitignore             single ignore file for the whole repo
└── README.md
```

`frontend/` and `backend/` never share a process, a dependency manifest, or a build.
They communicate over HTTP only.

## The 2026-09-09 restructure

The Next.js application was originally bootstrapped at the repository root — `src/`,
`public/`, `package.json` and every tooling config sat beside `.git/`. It was moved
wholesale into `frontend/`.

### How the move was done

- Tracked files were moved with `git mv`, so Git recorded 89 renames rather than
  delete-plus-add. `git log --follow <file>` still reaches pre-move history.
- `node_modules/` and `next-env.d.ts` were moved directly (they are ignored, not tracked).
- `.next/` and `tsconfig.tsbuildinfo` were deleted rather than moved — both are
  regenerable build artifacts, and the stale `.next` was actively causing a phantom
  build error (see below).
- **No frontend source file or config value was edited.** Every path in `tsconfig.json`
  (`@/* -> ./src/*`), `components.json` (`src/app/globals.css`) and `eslint.config.mjs`
  (`.next/**`) is relative, so all of them moved intact. `npm run build` produced the
  same 10 routes before and after.

### Things the move did break, and their fixes

| Broke | Fix |
| --- | --- |
| `.claude/launch.json` ran `npm run dev` from the repo root | added `"cwd": "${workspaceFolder}/frontend"` |
| `AGENTS.md` pointed at `node_modules/next/dist/docs/` | repointed to `frontend/node_modules/...` |
| `.gitignore` patterns were root-anchored (`/node_modules`, `/.next/`) | de-anchored so they match at any depth |

### Cost to in-flight branches

This repository had a dozen unmerged branches at the time of the move
(`feat/KP/initiative7`, `feat/nk/initiative8`, `feat/sj/init13`, `mockup/v2`, …).
Because every frontend path changed, **merging those branches will conflict**.

Recommended approach for each outstanding branch — rebase, do not merge:

```bash
git checkout feat/<branch>
git rebase feat/sj/codebase        # or whichever branch carries the restructure
```

Git's rename detection resolves most of it automatically, since the branches touch file
*contents* while the restructure only changed file *locations*. Resolve any remaining
conflicts by taking the branch's content at the new `frontend/` path. Landing these
branches sooner costs less than landing them later.

## Where things go

| Concern | Location |
| --- | --- |
| UI, routes, components, mock data | `frontend/src/` |
| Backend calls from the UI | `frontend/src/lib/api/client.ts` (single base URL) |
| API routes | `backend/app/api/` |
| Initiative business logic | `backend/app/initiatives/i7\|i8\|i13/` |
| Cross-initiative logic | `backend/app/shared/`, `backend/app/services/` |
| External systems | `backend/app/integrations/<platform>/` — one shared adapter each |
| Auth | `backend/app/core/security.py` — placeholder, nothing implemented |

## Known gotcha: stale `.next` cache

`npm run build` can fail with a type error naming a route that does not exist, e.g.
`Cannot find module '../../../src/app/actions/page.js'`. This is a stale
`.next/dev/types/validator.ts` left by a `next dev` run on a branch where that route
existed — not a real code error. Fix: delete `.next` and rebuild. With this many
branches carrying different route sets, expect it after branch switches.

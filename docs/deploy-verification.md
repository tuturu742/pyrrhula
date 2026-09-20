# Deployment verification runbook

Run this **every time the stack is redeployed** (image rebuild + migrate + recreate
api/worker). It proves the platform is healthy end-to-end against the live Ollama models, not
just that containers start. Loop until all scenarios pass.

## 0. Preconditions
- Stack up: `pyrrhula_postgres_1`, `pyrrhula_redis_1`, `pyrrhula_api_1`, `pyrrhula_worker_1`,
  `pyrrhula_admin` healthy; Ollama reachable at `http://ollama:11434`.
- Backend changes are baked into `pyrrhula:dev` (rebuild + `migrate` + recreate api/worker).
- **Web UI is a container too** (no host node/npm dependency): frontend changes need
  `podman build -t pyrrhula-web:dev -f docker/web.Dockerfile .` then
  `podman rm -f pyrrhula_web && podman run -d --name pyrrhula_web --restart always
  --network pyrrhula_default -p 5173:80 pyrrhula-web:dev`. nginx serves the production
  build and proxies `/api/*` to `pyrrhula_api_1:8000` with buffering OFF (SSE + the
  assistant chat's NDJSON depend on that — see docker/web-nginx.conf.template). For HMR while
  developing, run `npm run dev` in `web/` manually (Vite picks a free port).
  **After recreating `pyrrhula_api_1`, also `podman restart pyrrhula_web`** — nginx
  resolves the api container's IP at startup, so a recreated api leaves the proxy
  pointing at a dead address (symptom: the UI loads but every login/API call fails).
- API health: `curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/health` → `200`.
- **Exec environments** (delegated coding agents build + run tests in sibling containers):
  the worker needs the host podman socket —
  `systemctl --user enable --now podman.socket`, then run the worker with
  `-v /run/user/1000/podman/podman.sock:/var/run/podman.sock
  -e PYRRHULA_EXEC_SOCKET=/var/run/podman.sock --user 0` (socket is root-owned in the
  container). Without it the swe scenario still passes with `ci_status=pending`
  (direct-store fallback). **Environments reach the hosted repos over git smart-HTTP**
  (`/git/{store_key}/...`, short-lived job tokens; `PYRRHULA_GIT_HTTP_BASE`, default
  `http://pyrrhula_api_1:8000`) — no store volume mount anymore
  (`PYRRHULA_EXEC_STORE_VOLUME` is gone). Env containers need a network that carries
  the api: `podman network create pyrrhula-envs && podman network connect pyrrhula-envs
  pyrrhula_api_1`, and declare it on the engine
  (`PYRRHULA_EXEC_ENGINES='[{"key":"local","kind":"socket","socket":"/var/run/podman.sock","network":"pyrrhula-envs"}]'`
  on the worker). The store tree must be writable by the API's
  uid (10001): a store created before this change needs a one-time
  `podman exec pyrrhula_worker_1 chown -R 10001:10001 /app/data/blobs/repos`.
- **Web search** (per-persona switch): set `PYRRHULA_WEB_SEARCH_URL` on the api container to
  a SearXNG JSON endpoint reachable from it (dev: `http://searxng:8080`; that
  instance needs `search.formats: [html, json]` in its settings and working outbound DNS —
  custom podman networks may need `podman network update <net> --dns-add <upstream>`).
  Toggling a persona's "Web search" auto-registers the `web_search` MCP server on its
  workspace allowlist. Smoke: enable it on a persona, ask a directed turn to search — the
  message's `tool_calls_made` > 0 and the answer cites a URL.

## 1. After unit tests — prune test tenants
The pytest suite defaults to the live DB and leaves hundreds of throwaway tenants
(`agents-*`, `vocab-*`, `await-*`, …). Keep only the demo tenants this runbook seeds (+ the reserved library
tenant, which the purge always excludes):

```bash
# Dry run first — prints counts, changes nothing:
podman exec pyrrhula_api_1 python -m core.tenancy.purge \
  --slug-prefix '' --except dev --except rpg --except swe --except exec \
  --except gamedev --except secrets
# Then for real:
podman exec pyrrhula_api_1 python -m core.tenancy.purge \
  --slug-prefix '' --except dev --except rpg --except swe --except exec \
  --except gamedev --except secrets --yes
```
`--slug-prefix ''` matches every tenant; each `--except SLUG` keeps one. The api container
carries the superuser DSN (`PYRRHULA_DATABASE_URL`, role `pyrrhula`) the purge needs.

## 2. Seed + run the scenarios (idempotent)
`scripts/verify_deploy.py <scenario…>` seeds the tenant (via `scripts/seed_verification.py` run
inside the api container) and then drives the tenant owner through the HTTP API, asserting a
concrete outcome. Stdlib-only; run from the host:

```bash
python scripts/verify_deploy.py exec rpg swe   # the standing full check
python scripts/verify_deploy.py secrets        # the CORE claim: disclosure gate on live
                                               # turns; concealed plaintext provably absent
                                               # from context manifests and replies (INV-8)
python scripts/verify_deploy.py gamedev        # sidecar-backed game-dev path: tenant MCP
                                               # grants, real Godot CI + Web-export artifact
                                               # (needs the deploy/mcp-sidecars godot-mcp)
```

Seeding gives each tenant: a workspace, Ollama model connections (participants on `hermes3:8b`,
supervisor on `qwen2.5:7b` — a better instruction-follower for the synthesis deliverable; the
strong `qwen3.8:27b` is reserved for the swe coding build), a persona roster (1 supervisor + N
participants), an **overseer `workspace_membership` for the tenant owner** (session-acting is
gated on that — the owner does *not* get one automatically), and a fast single-round
`verify_round_table` flow (`max_rounds=1`, distinct from any user-facing flow). It also resets
the owner's local password to `verify-pass-123` so the harness can log in (login needs the
`X-Pyrrhula-Tenant: <slug>` header).

Scenario assertions:
- **exec** *(implemented, passing)* — an executive round-table on a new marketing campaign →
  reaches synthesis, 0 failed turns, and the synthesis is a clear campaign structure (asserts
  ≥3 of: target audience / core message / channels / phases-timeline / success metrics).
- **rpg** *(implemented, passing)* — a GM + 2 players; each player creates its character via `entity.create`
  (→ 2 character entities); one encounter resolves via `encounter_resolve` → a
  `ResolutionRecord` + ≥1 append to `entity_state_change` (a health-FSM transition).
- **swe** *(implemented, passing)* — the full moddable stack in one scenario: the seed
  creates + selects a **tenant workflow** (`swe-delivery`, cloned from the swdev template,
  repo access granted), registers **`babykb` in the repo registry** (node20 runtime + a real
  test command; the hosted store repo is imported from a podman-cp'd `~/code/babyKeyboard`
  via `file://` when present), and the session is created **bound to that repo**
  (`repo_ids`). The facilitator delegates the seeded work items; each becomes a real branch
  + PR on the tenant-namespaced server-side store, built **in the repo's exec environment**
  when the worker has the engine socket — the repo's test command runs there and its
  pass/fail is the PR's `ci_status` (asserted `passed` when envs are enabled). Then a
  review→fix: request changes on one PR → the agent reworks in the same environment, tests
  re-run, and a fix commit lands on the *same* branch. Delegating to a repo outside the
  session's selection is refused (409); archiving the session tears its environments down.
  **Real code generation is persona-bound — no env var**: the ASSIGNED dev persona's
  model connection writes the code (fallback: the session supervisor's connection; the
  `echo` test double falls back to the scaffold). Commit messages carry `[model]` vs
  `[scaffold]` provenance; any codegen failure falls back to the scaffold and logs
  `codegen.fallback_to_scaffold`. `PYRRHULA_CODEGEN_MODEL`/`_API_BASE` no longer exist.

**Assistant chat widget** (exec also asserts it): `POST /workspaces/{id}/assistant-chat`
streams NDJSON (text deltas / tool markers / edit **proposals** / done / error). The
assistant reads via viewer-scoped tools and only ever *proposes* writes — the browser's
Apply click runs the ordinary API call under the user's own session, so its access is
exactly the user's. History is browser-side only. The floating widget (bottom-right,
every page) is the front door; `/assist` (draft buttons) now has a 240s server timeout.

**Workspace assistant + repo knowledge graph** (both scenarios extend automatically):
- exec also proves the **required assistant**: `GET /workspaces/{id}/assistant` ensures the
  `informational` persona exists (its model profile is seeded from
  `PYRRHULA_ASSISTANT_MODEL`, which is **empty** on a clean install -- set it, or point the
  "Assistant model" connection at something yourself, before expecting a draft back), and a
  `POST /assist` `draft_persona` call must return a real draft
  (grounded in the caller's entitled workspace knowledge; metered `rewrite`/`generation`).
- swe also runs **repo analysis**: `POST /workspaces/{id}/repo-analysis` → worker job
  `analyze_workspace_repos` reads the hosted store trees, ingests each repo as knowledge,
  and writes per-repo summaries + a cross-repo overview + a `RepoGraph` JSON entry into the
  `repo-overview-*` knowledge source. The harness polls `GET /repo-graph` until
  `available` with ≥1 node and a non-empty overview. The UI renders it at
  `/workspaces/{id}/repo-graph` (React Flow).

## 3. Loop
Re-run until all requested scenarios pass. Archive the scenario sessions afterward (soft-delete,
reversible) so the demo tenants stay tidy for the next redeploy.

> Do **not** run the full pytest suite against the live DB — it pollutes it (that is what step 1
> cleans up). Use targeted `tests/architecture/` + `tests/isolation/` + the live harness.

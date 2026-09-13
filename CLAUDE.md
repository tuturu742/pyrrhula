# CLAUDE.md — ground rules for coding agents

Pyrrhula is a multi-tenant, multi-agent orchestration platform (tabletop-RPG-first,
enterprise-second, software-development-third — D15) built around one claim: **who-knows-what
is enforced by the system, not requested of the model.** Read `docs/agent-guide.md` before
your first implementation task.

## Source-of-truth order

1. `pyrrhula-development-plan.md` (v1.2) — **authoritative.** Decisions D1–D15 and open-question
   resolutions Q1–Q6 are final. Do not re-litigate them; do not "improve" on them.
2. `tasks/<phase>/<task>.md` — the task you were assigned. Its acceptance criteria define done.
3. `docs/agent-guide.md` — conventions and definitions.
4. `pyrrhula-research-brief.md` — background only. Where it conflicts with the plan, the plan wins.

## Hard rules (violating any of these fails review)

1. **Vocabulary.** If you are about to write `game_master`, `dice`, `campaign`, `character`,
   `player`, any RPG word — or any software-development word (`sprint`, `standup`, `engineer`,
   `pull_request`, `commit`, `branch`, …) — in a schema, table name, API path, or module under
   `packages/core/` — stop. Core code uses domain-neutral terms and emits `label_key`s; domain
   words live only in workflow-plugin pack content (`.plugins/`, see `deploy/plugins.json`) and `vocabulary_overlay` data. See the glossary in
   `docs/agent-guide.md`.
2. **INV-1.** No module outside `core/assembler/` and `core/overseer/` may import
   `core/knowledge/repo` or `core/secrets/repo`. The import-graph lint enforces this; never
   weaken or bypass the lint, never add an exemption.
3. **INV-2.** `ContextAssembler.assemble(viewer: Principal, phase: Phase, ...)` — both
   required, no defaults, no `Optional`. Same for `scope_key` on every vector/lexical query
   (INV-4): required, defaultless, pushed down as a SQL predicate — post-filtering is forbidden.
4. **Tenancy.** Every tenant-scoped table gets `tenant_id` + RLS with `FORCE`. Database
   sessions are opened **only** through `core.tenancy.scope.tenant_scope()` (or, for tables
   that aren't tenant-isolated at all — `tenant`, `role_permission`, `plugin_repository` (deployment-level, admin-writable only), and `job` (a worker must
   claim work across every tenant) — `unscoped_session()`), which sets `app.tenant_id` with
   `set_config(..., is_local => true)` (transaction-local —
   session-level GUCs leak through connection poolers). RLS policies compare against
   `NULLIF(current_setting('app.tenant_id', true), '')::uuid`, not a bare cast: Postgres resets
   a transaction-local custom GUC to `''` (not NULL) once its transaction ends, so a bare
   `::uuid` cast raises instead of failing safe to zero rows. Any new tenant-scoped table must
   be added to the isolation negative-test suite (`tests/isolation/`) and use this same policy
   shape in the same PR. Only `core/tenancy/scope.py` may construct a SQLAlchemy engine or
   sessionmaker (`tests/architecture/test_single_session_opener.py` enforces this).
5. **Append-only tables** (`audit_log`, `session_event`, `resolution_record`,
   `secret_disclosure_event`, `disclosure_decision`, `knowledge_source_version`,
   `entity_state_change`, `context_manifest`, `checkpoint`): the app DB role has no
   UPDATE/DELETE grant. Never add one. Hash-chained tables compute
   `row_hash = sha256(prev_hash || canonical(row))`.
6. **Deterministic results render from the record** (INV-7). UI and reports read
   `ResolutionRecord` by id; model prose about a result is decoration, never a source.
7. **Secrets.** Concealed secret plaintext must be absent from the generation context
   (INV-8) — exclusion, not instruction. The disclosure gate sees gists only, never
   `secret.content`. Gate failure fails **closed to conceal**. Overseer reads of secret
   plaintext write the audit row in the same transaction (INV-5).
8. **Idempotency.** Every side-effecting operation (model call, tool exec, entity mutation,
   external MCP call) takes an idempotency key `(session_id, event_seq, attempt_target)` and
   checks `completed_operation` first. Resume re-executes; without this we double-charge
   tenants' API keys.
9. **Packs are content, not code.** pack content lives in workflow-plugin repos (pinned via `deploy/plugins.json`, fetched to `.plugins/` -- run `scripts/fetch_plugins.py`) and may not import from `packages/core/`. If a pack
   needs a core import, the core is missing an abstraction — raise it, don't work around it.
10. **No user-authored code, ever.** User-supplied logic is JSON Schema + declarative FSMs +
    CEL expressions only. No `eval`, no RestrictedPython, no exceptions (D7).
11. **Usage metering** (`usage_record`) is written in the same transaction as the message it
    meters, with a `purpose` value (generation | gate | rerank | embed | report | rewrite |
    delegation — the last added in v1.2 for D15 delegated coding-agent work). The egress
    policy check lives inside the `ModelProvider` port, keyed on the same `purpose` taxonomy
    (D14); delegated MCP work is egress-controlled by the workspace MCP **allowlist**, not by
    D14 — do not extend D14 to cover it.
12. **Ports, not features.** Cross-cutting concerns go through the ports in `core/ports/`
    (`PermissionService`, `IdentityProvider`, `TenantRouter`, `VectorStore`, `ModelProvider`,
    `JobQueue`, `BlobStore`, `Encryptor`, `ModerationProvider`) with trivial v1
    implementations in `packages/adapters/`. Call `PermissionService.check(principal, action,
    resource)` at call sites; never inline role logic.

## Stack

Backend: Python 3.12, FastAPI, SQLAlchemy 2.0 async + Alembic, Pydantic v2, celpy, httpx,
structlog, OpenTelemetry. Data: PostgreSQL 16 (pgvector, JSONB, tsvector, SKIP LOCKED job
queue) + Redis + S3-compatible blobs. Frontend: React 18 + Vite + TypeScript, Tailwind +
shadcn/ui, React Flow, TanStack Query, Zustand; API client generated via `openapi-typescript`.
Model access exclusively through the `ModelProvider` port (LiteLLM adapter). Repo layout:
plan Appendix B — do not invent alternative structure.

## Workflow

- Work from a task file in `tasks/`. Check its `Depends on:` tasks are `done` first. Default:
  one task = one branch = one PR. Update the task file's checkboxes and status line in the PR.
- Assigned a whole phase or track to work sequentially instead (a human says so explicitly)?
  Follow `docs/phase-workflow.md`: one branch for the track, one commit per task in
  dependency/numeric order, one PR at the end — not one task = one branch = one PR.
- CI-blocking suites you must keep green and must extend when relevant:
  `tests/isolation/` (T0.4), `tests/architecture/` (INV-1 lint), `tests/replay/` (INV-10),
  `tests/packs/` (INV-9, from Phase 3), `tests/leak/` (from Phase 2).
- Acceptance criteria are falsifiable on purpose. If a criterion is untestable as written,
  say so in the PR rather than quietly reinterpreting it.
- Never commit provider API keys; `model_profile.credential_ref` points into a secret
  manager, never holds a key.

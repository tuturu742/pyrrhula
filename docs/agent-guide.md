# Pyrrhula — Implementer's Guide for Coding Agents

The definitions, rules, and conventions an implementing agent needs. Together with
`CLAUDE.md`'s hard rules, this is the reference.

---

## 1. What is being built

A self-hosted or SaaS, **multi-tenant from day one** platform for structured multi-agent
conversations. Knowledge, process, and participant state are versioned structured data; the
conversation is driven by an explicit phase/turn engine. First market: AI-managed tabletop
RPG campaigns. Second (same engine, different overlay + pack): enterprise multi-agent
workflows. Third (same pattern): **multi-agent software development** — a facilitator
agent as engineering manager proposing tasks, participant agents as engineers on a shared
repository, implementation delegated to external coding agents over MCP; end state is
dogfooding (Pyrrhula's own backlog worked by a Pyrrhula-managed team). One product,
no separate SKU.

The organizing principle of the architecture:

> **There is exactly one code path that decides what text reaches a model, and it takes a
> principal as a required argument.** (`assemble(viewer, phase, ...)`)

Everything else protects that invariant.

## 2. Design principles — settled, not up for re-litigation

These are the decisions the codebase is built on. They are stated as rules because each
one closes a question a contributor would otherwise reopen; the reasoning is inline so
the rule can be checked against it rather than taken on faith.

- **The process engine is custom-built**: an interpreter over a declarative, versioned
  JSON DSL. Not a graph library — a library's graphs are code, and ours are user data
  that must import, export, version and diff. The useful concepts are borrowed: a
  session cursor, a checkpoint per transition, await/resume, fork-from-checkpoint.
- **Rule-versus-lore priority is budget allocation**, never score multipliers: per-class
  token quotas per phase, fused by weighted reciprocal rank. A multiplier is a claim
  about relevance; a budget is a claim about how much room each class gets, and only
  the second is something an author can reason about.
- **Secrets are a first-class record** with a holder set and a disclosure state machine
  — not a field on an entity or a persona.
- **Secret-leak control is context exclusion, not instruction.** The gate decides; the
  assembler removes the plaintext. Nothing asks the model to keep a secret it can see.
  This is the single most important design claim in the system.
- **Deterministic results render from the record**, never from model text. A die roll,
  a rule check, a lab result: the UI and reports read the `ResolutionRecord`; whatever
  the model says about it is decoration.
- **Two tool abstractions**: a `DeterministicTool` is pure and replayable; an
  `EffectfulAction` has side effects and can suspend. Approval routing is a process-engine
  await, not a tool.
- **Entity schemas are a JSON Schema 2020-12 subset plus a declarative FSM plus CEL.**
  No user-authored code, ever. Semantic tags drive rendering.
- **One Postgres 16** for relational data, vectors (pgvector), JSONB, lexical search
  (tsvector) and the job queue. Another vector store is a designed-for swap behind the
  `VectorStore` port, not a dependency.
- **Python 3.12 + FastAPI** backend; **React 18 + Vite + TypeScript** frontend.
- **SSE for streaming, POST for commands.** WebSockets only where multi-participant
  presence genuinely demands them.
- **Ports with trivial first implementations** for permissions, identity, tenant routing,
  vector store, models, jobs, blobs, encryption, moderation — from day one, so the
  composition roots are the only place an adapter is chosen.
- **The leak-evaluation harness ships before any high-stakes behavioural axis.** A
  malice or deception slider is gated on measured results per provider, not on intent.
- **Pack seed content lives in a read-only library tenant**; the knowledge RLS policy
  has exactly one named exception for it. Tenants fork on edit.
- **Egress policy is per tenant and keyed on `purpose`**, enforced inside the
  `ModelProvider` port. Delegated MCP work is governed by the workspace MCP allowlist
  instead — a different mechanism on purpose; do not extend one to cover the other.
- **Software development is "pack plus delegation".** The swdev workflow plugin and its
  vocabulary overlay; repositories ingest as knowledge pinned to a commit; engineering
  side effects are MCP effectful actions; the code itself is written and run by external
  coding agents inside isolated execution environments the platform provisions, briefed
  through the same assembler under the dispatching principal's visibility. Pyrrhula
  never runs or edits code inside its own process.

Resolved product questions, for the same reason: one product, not a family; **no pack
marketplace** (bundles are shared out of band and scanned for injection at import);
**no cross-tenant knowledge sharing** (the library tenant is the sole exception);
contradicting narration gets a **badge, not a rewrite**.

## 3. The invariants (each has a CI test)

| ID | Invariant | Enforcement |
|---|---|---|
| INV-1 | No stored text reaches a model except through `core.assembler.context_assembler.assemble()` | import-graph lint: only `core/assembler/` and `core/overseer/` may import knowledge/secrets repos |
| INV-2 | `assemble()` requires `Principal` and `phase` — no defaults | type signature |
| INV-3 | Tenant filtering happens in the database | RLS `FORCE` + filter-omission negative tests |
| INV-4 | Every vector query carries required `scope_keys` and `class_` filters, pushed down | defaultless port parameter + pushdown test |
| INV-5 | Overseer secret reads write an audit row in the same transaction | single read path in `OverseerService` |
| INV-6 | Audit log append-only, tamper-evident | no UPDATE/DELETE grant + `prev_hash` chain + verifier job |
| INV-7 | Mechanical results shown to users come from `ResolutionRecord`, never model prose | UI reads record by id |
| INV-8 | Concealed secret plaintext absent from generation context | assembler exclusion step + leak-eval harness |
| INV-9 | Every shipped pack (`default`, `rpg`, `swdev`) loads with zero core changes | all-packs CI smoke test |
| INV-10 | Any turn replays identically from its `ContextManifest` + `ResolutionRecord`s | replay test |

## 4. Glossary (core term → RPG / enterprise / swdev overlay)

Core code and schemas use the left column and emit `label_key`s; UIs resolve labels through
`vocabulary_overlay`. Nothing in the database is ever renamed.

| Core term | RPG (`rpg_v1`) | Default / enterprise (`default_v1`; labels illustrative — the shipped overlay sets only the persona types, *Facilitator* and *Contributor*) | swdev (`swdev_v1`) |
|---|---|---|---|
| Tenant | Account | Organisation | Organisation |
| Workspace | World / Campaign | Workspace | Project |
| Process Definition / Phase | Session Flow / Scene, Turn | Workflow / Stage | Engineering Workflow / Stage |
| Facilitator / Participant / Informational Agent | Arbiter / PC-NPC bot / Oracle | Chair / Domain Expert / Reference Agent | Engineering Manager / Engineer / Codebase Expert |
| Human Participant / Overseer | Player / Director | Contributor / Compliance Reviewer | Team Member / Engineering Director |
| Knowledge Source: class `rules`, `lore`, `misc` | Rulebook, Lorebook, Miscellany | Policy Doc, Domain Context, Reference | Engineering Standards (ADRs, conventions), Architecture & Product Docs, Reference |
| Entity / Entity Schema / State Machine | Character / Sheet Template / Status Track | Ticket / Record Type / Lifecycle | Work Item / Item Template / Lifecycle |
| Deterministic Tool / Effectful Action | Dice Roller / Table Action | Calculator, Policy Lookup / Approval Routing | Checklist Runner / Delegated Operation |
| Resolution Record / Rule System | Roll Result / Game System | Calculation Record / Policy Framework | Check Result / Engineering Playbook |
| Scope / Secret | GM-only, Faction / Hidden Motive | Need-to-know / Confidential Info, MNPI | Access Area (Maintainers-only) / Embargoed Info |
| Behavior Profile / Disclosure Decision | Personality Dials / (hidden) | Disposition Settings / Decision Record | Working Style / Access Decision |
| Context Manifest / Checkpoint / Report / Pack | What the Arbiter Knew / Save Point / Recap / Game System Pack | Evidence Basis / Snapshot / Minutes / Domain Pack | Briefing Basis / Snapshot / Status Report / Practice Pack |

**Forbidden words in core code, schemas, table names, and API paths:** `game_master`, `gm`,
`dice`, `campaign`, `character`, `player`, `spell`, `npc`, and any other RPG term — plus
swdev terms: `sprint`, `standup`, `engineer`, `pull_request`. They exist only
in workflow-plugin content (`.plugins/`, pinned in `deploy/plugins.json`) and overlay label
data. ("Standup"/"Work Item Triage" are process-template names shipped by the swdev pack,
and "Sprint Recap" an overlay label; `pull_request` and `build` are pack entity *schemas*, not core nouns. "Embargoed
Info" covers undisclosed vulns/incidents/plans — never tool credentials, which stay
`credential_ref`s.)

`tests/architecture/test_vocabulary_lint.py` enforces this on both halves: the RPG
overlay's display strings in `web/src`, and the unambiguous forbidden words in
`packages/core`. It scanned only the frontend until the backend scan found four real
violations that had been shipping for months — `dice_roller`, `dice_grammar`,
`max_dice_count`, `campaign_recap`. A line may carry a `vocab-ok:` marker with a reason,
and there is exactly one honest use: a foreign key we call rather than coin (an MCP
server's own tool name). Prose that wants the marker should be reworded instead.

**`repo`, `git`, `commit` and `branch` are core vocabulary, not forbidden.** They were on
the banned list and the code has long since disagreed: `repo` and `session_repo` are core
tables, `/repos` and `/git` are core API prefixes, and a hosted git store is a core
capability rather than a domain metaphor. A repository is what it is in every domain this
platform serves — there is no neutral synonym to reach for, which is the test a forbidden
word has to fail.

## 5. Architecture map

```
Clients (Web UI · Overseer Console · Public API · MCP clients)
  → Edge (AuthN → Principal, rate limit, tenant resolve)
  → FastAPI app:
      Authoring API · Session API · Overseer API (separate router)
      Process Engine (interpreter, scheduler, checkpoints, awaits)
      ★ Context Assembler (the ONLY stored-text→model path)
      Knowledge Service · Agent Runtime · Resolution Service
      Entity/FSM Service · Secrets Service · Audit Service
      Ports: Permission · Identity · TenantRouter · VectorStore ·
             ModelProvider · JobQueue · BlobStore · Encryptor · Moderation ·
             Embedding · Reranker · ExecEnv · McpTransport · Notifier · Preview
  → Workers (same image: ingestion, embedding, async turns, reports, eval)
  → PostgreSQL 16 (RLS FORCE) · Redis · Blob store
  → External: Ollama / OpenAI / Anthropic / Gemini · MCP servers
```

### The Context Assembler's 10 steps

1. Resolve visible scope keys for (viewer, phase, session) — hard filter.
2. Split the phase's token budget across knowledge classes by `phase.budget.ratio`.
3. Per-class hybrid retrieve (dense + sparse + keyword activation), scoped, pushed down.
4. Fuse within class via weighted RRF (k=60; weights dense 1.0 / sparse 0.8 / keyed 1.2).
5. Rerank within class (class-blind cross-encoder, top-32 → 16).
6. Fill buckets — `constant` entries first, then ranked, spill per policy.
7. Inject entity state deterministically (**never retrieved**).
8. Apply the disclosure gate: conceal → remove plaintext + inject `behavioral_directive`;
   hint → inject `hint_text`; reveal → inject + disclosure event + holder update.
9. Order context stable → volatile (prompt-cache layout; a volatile token near the top
   invalidates the whole cached prefix).
10. Emit `ContextManifest` (entries, ranks, why, redactions, hashes) — persisted.

### The deterministic trust chain

Model emits a *request* → `ResolutionService` validates expression and modifiers against the
rule system and **actual entity state** (the model never asserts its own modifier) → seeded
execution (`seed = HMAC(session_secret, session_id || event_seq || expression)`) → immutable,
hash-chained `ResolutionRecord` → injected into context as a system-authored fact → **UI
renders from the record** → best-effort contradiction check flags (badge, no regen).

### Secrets pipeline

Should-fire prefilter (no model call: holder has in-scope secrets ∧ phase not `mechanical` ∧
gist topicality above τ) → disclosure gate (one structured call on a small model, input =
gists + behavior axes + recent turns; output validated against strict JSON schema; persisted
as `DisclosureDecision`; **fail closed to conceal**) → context construction per decision →
generate → post-generation leak check (fuzzy + embedding match vs concealed plaintext;
regenerate once, then safe fallback + overseer alert).

## 6. Data model quick reference

Tenant-scoped tables carry `tenant_id` + RLS; append-only tables (marked `‡` below) have
no UPDATE/DELETE grant.

### What a session carries, and what the workspace keeps

**A persona's state lives in the workspace; only its conversation belongs to the session.**
This is deliberate — a table's characters are still there next week, a backlog outlives the
standup that made it — and it is the single most surprising thing about starting a second
session in a workspace, so it is written here rather than discovered.

| Workspace-scoped — every later session sees it | Session-scoped — one session only |
|---|---|
| `entity` (characters, work items, anything a schema defines) | `message` (the transcript) |
| `persona`, **including its `entity_id` binding** | `session_event` (the durable log) |
| `scope` (the visibility bands) | `resolution_record` (what the dice actually said) |
| `secret` (private briefs) | `context_manifest` (what a turn was given) |
| `workspace_knowledge_attachment` (sources, version pins) | `entity_state_change`‡ |

So a persona starting a second session remembers nothing that was *said* and keeps
everything it *has*. `core.entities.injection` renders entity state from
`list_entities_for_scope`, which filters on tenant + workspace + `scope_key` and **has no
session filter** — every entity the viewer's scopes admit is in every turn's context, for
as long as the workspace exists.

The consequences are worth stating plainly, because two of them read as model failures:

- A second campaign in the same workspace opens with the first campaign's characters in
  context, and a player who reads them will reasonably conclude it already has a sheet and
  decline to roll a new one.
- `persona.entity_id` survives its session, so `entity_create` with `bind_to_self` finds
  the persona already bound and refuses.
- Archiving a session does **not** remove the entities it created. Archiving hides the
  session; the workspace keeps what the session made.

Every entity records the session that created it (`entity.origin_session_id`), so "retire
what that session made" is answerable — but nothing does it automatically, and there is no
delete-entity path at all. **A genuinely fresh start means a fresh workspace (or a purged
tenant), not an archived session.**

- **Tenancy/identity:** `tenant`, `principal` (humans, service accounts, *and agents*),
  `identity`, `membership`, `workspace_membership` (steward|facilitator|participant|
  overseer|viewer — `steward` is the solo creator's combined facilitator+overseer seat and
  is what signup and workspace creation assign; the implication lives in
  `core.tenancy.roles`, never re-derived at a call site), `role_permission`.
- **Workspace/process:** `workspace`, `vocabulary_overlay`, `process_definition` (DSL JSONB,
  versioned).
- **Knowledge:** `knowledge_source` → `knowledge_source_version`‡ (content-addressed, DAG) →
  `knowledge_entry` (stable `entry_key`, activation fields) → `knowledge_chunk` (denormalised
  `class` + `scope_key` for pushdown; `vector(1024)` HNSW; generated `tsv`);
  `workspace_knowledge_attachment` (version pin, scope, priority — the workspace's opinion
  lives here, not on the source).
- **Agents:** `persona` (is a principal; `persona_type` supervisor|participant|informational),
  `agent` (provider, params, `credential_ref` — never the key), `behavior_profile`‡
  (immutable versions), `axis_definition` (pack content; `stakes: high` requires a gate binding).
  Two renames landed after this list was written and both moved a name onto a different
  thing, so older prose reads as correct while naming the wrong table: what was `agent` is
  now `persona`, and what was `model_profile` is now `agent`. There is no `model_profile`
  table.
- **Entities:** `entity_schema` (fields/derived/FSMs/views/constraints JSONB), `entity`
  (JSONB data + generated columns for `indexed:true` fields), `entity_state_change`‡.
- **Scopes/secrets:** `scope`, `secret` (content, gist + embedding, `hint_text`,
  `behavioral_directive` ★, disclosure_state, `publication` guarded|publishable — whether
  this plaintext may leave the deployment in the clear; defaults closed and gates only
  the export's encryption requirement, never who may see it), `secret_holder`,
  `secret_disclosure_event`‡, `disclosure_decision`‡.
- **Sessions:** `session` (optimistic `version`, one advancing writer via FOR UPDATE),
  `session_event`‡, `message`, `context_manifest`‡, `checkpoint`‡, `await_state`,
  `resolution_record`‡ (hash chain), `completed_operation` (idempotency).
- **Audit/usage:** `audit_log`‡ (hash chain), `usage_record` (same transaction as the
  message; `purpose` ∈ generation|gate|rerank|embed|report|rewrite|delegation — `delegation`
  added for delegated coding-agent work), `price_table`, `report`.

## 7. Coding conventions

### Backend
- Python 3.12, `ruff` + `mypy --strict` on `packages/`; FastAPI routers per resource;
  SQLAlchemy 2.0 async ORM; Alembic migrations (one per PR max, autogenerate then hand-check).
- Pydantic v2 models for all API IO and for the ProcessDefinition/EntitySchema DSLs.
- DB sessions only via `tenancy/scope.py::tenant_scope()`. New tenant-scoped tables: add RLS
  in the same migration + cases in `tests/isolation/` in the same PR.
- Structured logging via `structlog`; OpenTelemetry spans around every turn, retrieval, and
  provider call from day one (per-turn traces are how context assembly gets debugged).
- Jobs: Postgres `SELECT ... FOR UPDATE SKIP LOCKED` via the `JobQueue` port. Workers run the
  same image with a different entrypoint.
- CEL via `celpy`; compile-check user expressions at save time, evaluate with bounded inputs.

### Frontend
- Feature folders under `web/src/features/`; API types generated from OpenAPI
  (`openapi-typescript`) — never hand-write response types.
- All user-facing strings for core nouns go through the vocabulary resolver
  (`web/src/lib/vocabulary/`), keyed by `label_key`. Hardcoding "Arbiter" or "Rulebook" in a
  component is a bug.
- SSE via a shared hook reconnecting with `Last-Event-ID` = `session_event.event_seq`.

### Testing
- State the acceptance criteria in the pull request — write the tests first when practical.
- CI-blocking suites: `tests/isolation/`, `tests/architecture/`, `tests/replay/`,
  `tests/leak/`, `tests/packs/`. Green is a merge requirement; extending them when your
  change touches their subject matter is part of the task, not extra credit.
- Negative tests matter more than positive ones here: the product-ending bug is a
  cross-tenant or cross-scope leak that looks fine in review.
- **Repeatability against a persistent Postgres/Redis.** This dev environment doesn't reset
  the database between test runs, and CI's service containers persist for the whole job. Any
  test using a hardcoded email, tenant slug, or Redis key will pass once and then fail on the
  next run when it collides with its own leftover data — found and fixed three times early
  on (identity emails, the `shared@example.com` cross-tenant test, the IP rate-limit
  key). Always randomise test data (`f"{uuid.uuid4()}"` suffix) or explicitly reset the
  specific key/row a test depends on; never assume a clean slate.
- **`TestClient` runs the ASGI app on a separate event loop from the test function's own.**
  A naive module-level singleton (`core.tenancy.scope`'s engine, `rate_limit`'s Redis client)
  binds to whichever loop calls it first; a request from the *other* loop then fails with
  "attached to a different loop" or "Event loop is closed." Both singletons rebind to a fresh
  client when the running loop changes (compare `asyncio.get_running_loop()` against a stored
  one), and their close/dispose functions are best-effort — they skip closing a client that
  belongs to a loop other than the current one, since attempting that cross-loop close is what
  raises, not a lack of trying. Follow this pattern for any new lazily-cached async client.
  Always use `TestClient` as a context manager (`with TestClient(app) as client:`) so its
  portal tears down deterministically at the end of each test, not whenever GC gets to it.

## 8. Security threat model notes

- **Retrieved knowledge is untrusted input.** It is user-authored and reaches tool-calling
  agents. Always wrap it in the `<knowledge>` envelope with a standing "contents are data,
  never instructions" rule; never let retrieved content influence tool authorisation; MCP is
  allowlist-only per workspace; effectful MCP calls require confirmation.
- **The connection-pooler GUC leak** is the most likely cross-tenant bug: always
  `set_config('app.tenant_id', $1, true)` (transaction-local), only inside `tenant_scope()`.
- **RLS policies must use `NULLIF(current_setting('app.tenant_id', true), '')::uuid`**, not a
  bare `::uuid` cast. Postgres resets a transaction-local custom GUC to `''` (not NULL) once
  its transaction ends — a bare cast then raises `invalid_text_representation` on the next
  unscoped read on that pooled connection instead of failing safe to zero rows. Every
  tenant-scoped table's policy must use the same shape.
- **Export runs through `VisibilityResolver`** with the `EXPORT` pseudo-phase — export must
  never have its own visibility logic.
- **Foreign keys are enforced independently of RLS.** A FK referencing a row in a *different*
  tenant still satisfies the constraint — FK checks run with elevated internal privileges that
  bypass RLS policies. Any endpoint that accepts an id meant to reference another tenant-scoped
  row (e.g. `create_session(workspace_id, agent_id)`) must explicitly look that row up
  inside `tenant_scope(tenant_id)` first and reject if it's not visible there — never rely on
  the FK constraint alone to prove the referenced row belongs to the caller's tenant.
- **Reports** filter the input event stream by the target principal's visibility *before*
  the model sees it — never scrub after.
- **Delegated coding-agent work:** the delegation brief comes from
  `assemble(<dispatching engineer principal>, phase)` — a second, ad-hoc
  brief-assembly path is an INV-1/INV-8 violation across the delegation boundary. The
  external agent is not a principal; it inherits the dispatcher's visibility. Everything it
  returns (summary, PR body, diff excerpts) is untrusted input: enveloped, never
  instruction-bearing, never influencing tool authorisation. Branch/PR/CI state renders from
  the `EffectfulAction` outcome record, never from model prose (INV-7). Repo content ingested
  as knowledge is attacker-controlled text: it runs the import-time injection scan and a
  declarative secret-pattern scan (quarantine for overseer review — posture, not INV-8
  coverage; the assembler cannot exclude a secret nobody registered).
- The library tenant is the **only** permitted foreign-tenant read, tested as a full matrix
  in `tests/isolation/`. Never add a second unnamed exception.

## 9. Definition of done (any task)

1. The acceptance criteria stated in the pull request demonstrably met (tests or
   reproducible steps).
2. CI green, including the blocking suites; new tenant-scoped tables covered in isolation tests.
3. No forbidden vocabulary in core; no new imports violating INV-1; no defaults added to
   principal/phase/scope parameters.
4. Migrations forward-only and applied from an empty database in CI; append-only grants
   intact.
5. Deviations from the stated criteria noted honestly in the pull request.

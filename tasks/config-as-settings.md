# CFG — Deployment config becomes tenant/workspace settings

**Track:** Config · **Status:** done
**Plan refs:** §12.2 (the overlay fallback chain this reuses), §13.9 (D14 reads
`tenant.settings` already), D11 (ports — an adapter reads a resolved value, never an env).

## The rule

> **If two tenants might reasonably want different values, it is not an environment
> variable.** It is a workspace / tenant / connection / MCP-server setting, reachable in
> the UI. An environment variable is for *deployment facts* — where the database is, what
> this host can reach — and, where a value must exist before any tenant has an opinion,
> for the **system default at the bottom of a fallback chain**.

Resolution order, matching the vocabulary overlay precedent (`core/vocabulary/service.py`):

```
workspace.settings  ->  tenant.settings  ->  system default (core/config.py)
```

A limit a user has to discover as an environment variable is a limit they will never set.
This task exists because two real bugs came from breaking the rule: the sample lab's own
call budget silently starved concurrent sessions, and `PYRRHULA_OLLAMA_NUM_CTX` could not
be overridden per connection at all — it raised `dict() got multiple values for keyword
argument` on every turn for the one tenant whose hardware justified setting it.

## Already done (this is the pattern to copy)

- [x] **MCP per-server limits** — `max_calls_per_session`, `timeout_seconds`,
      `max_result_chars` on `mcp_server`, NULL = platform default, surfaced on the
      workspace's MCP card. Removed `PYRRHULA_REMOTE_MCP_TIMEOUT_S` and
      `PYRRHULA_REMOTE_MCP_MAX_RESULT_CHARS`. (`5f7c3f1`, migration `f3b8d2e41a97`)
- [x] **Connection/persona params beat platform defaults** — precedence is request →
      persona → connection → default. (`f4117b7`)
- [x] **The audit itself** — `docs/configuration.md`, every variable listed and checked
      mechanically against the code. (`25096c1`)

## Subtasks

- [x] **CFG.1 — the resolver.** `core/settings/resolve.py`:
      `resolved_setting(tenant_id, workspace_id, key, default)` reading
      `workspace.settings` → `tenant.settings` → the passed default, plus a typed
      accessor per group below. One implementation; no call site re-derives the chain.
      Unit tests for each layer winning, and for a workspace value of `""`/`0`/`false`
      counting as *set* rather than falling through.
- [x] **CFG.2 — model selection.** Narrowed by inspection, most of it already correct:
      - *gate* — already a **tenant** choice (`core/secrets/gate_config.py`) naming one of
        the tenant's own connections, with the env as the system default beneath it.
        Correct as it stands. Nearly deleted as "dead" because the factory reads it via
        `getattr(settings, "gate_model", "")`, which a grep for `settings.gate_model`
        misses and which degrades *silently* — `scripts/check_env_docs.py` caught it.
      - *embedding / reranker* — deployment-level on purpose and already admin-console
        editable (`core/deployment_settings.py`): one vector column of one width, and the
        providers hold a loaded model in memory.
      - *assistant* — a cold-start seed only; the profile it creates is ordinary editable
        tenant data afterwards. Correct as it stands.
      - [x] *moderation* — resolved per tenant/workspace **without a port change**. The
        port was never the right seam: `check()` stays tenant-agnostic and the composition
        root resolves which adapter to build, which is where a selection decision belongs.
        `core/moderation_selection.py`; the API dependency and the direct-call form are
        kept separate so a tenant id cannot bind to an injected request context.
- [x] **CFG.3 — budget knobs onto the connection.** `PYRRHULA_HISTORY_CHAR_BUDGET` and
      `PYRRHULA_CODEGEN_MAX_TOKENS` become connection params (`max_tokens` already
      merges; history budget needs a named key), env value demoted to system default.
      Both are justified in code comments by *which model on what hardware* — the
      definition of per-connection.
- [x] **CFG.4 — `PYRRHULA_MAX_REVIEW_ROUNDS` → workspace setting.** How many times a
      reviewer may send work back is workflow policy, the same kind of decision as how
      many rounds an interrogation runs. Keep a deployment ceiling as the system default
      so a workspace cannot set it unbounded.
- [x] **CFG.5 — web search onto the registration.** Via a generic `options` JSONB bag on
      `mcp_server`, so one transport's vocabulary never lands in the generic registration
      and the next knob costs no migration.
- [x] ~~CFG.5 (original wording)~~ `PYRRHULA_WEB_SEARCH_ENGINES` belongs
      on the `web_search` MCP server row, next to its URL, not in the environment.
- [x] **CFG.6 — demote non-settings to constants.** `PYRRHULA_EMPTY_RETRY_TOKEN_FACTOR`
      and `PYRRHULA_REASONING_MIN_COMPLETION_TOKENS` are repair heuristics no tenant
      should need to differ on. If they are not settings they are not env vars either:
      make them module constants.
- [x] **CFG.7 — UI.** A workspace settings panel for CFG.2/CFG.4 (the MCP card already
      covers its own fields). Empty input = inherit, shown as the inherited value in
      placeholder text so "unset" is never mistaken for "zero".
- [x] **CFG.8 — docs + a CI guard.** `scripts/check_env_docs.py` runs in the lint job:
      every variable the code reads has a table row, and every row is still read. Prose
      may name a retired variable as history without it counting as live.
- [x] **CFG.8b — remaining docs.** `docs/configuration.md` updated as each group lands; the
      coverage cross-check re-run so no variable is documented that no longer exists.

## Acceptance criteria

1. `docs/configuration.md` lists every environment variable the code reads, verified by
   the mechanical cross-check (grep of `packages/ scripts/ deploy/ docker/ install.sh`
   plus `Settings` fields, diffed against the doc) — no undocumented variable, no
   documented variable absent from the code.
2. Every variable remaining in the doc is either a deployment fact, a system default at
   the bottom of a resolution chain, or explicitly justified as un-varying (see
   `EMBEDDING_DIMENSION` below).
3. Setting a value on a workspace overrides the tenant value, which overrides the system
   default — proven by a test per group, not by inspection.
4. An unset workspace value inherits rather than writing a zero.
5. The CI-blocking suites stay green; `tests/isolation/` gains coverage for any new
   tenant-scoped column.

## Deliberate exception

**`PYRRHULA_EMBEDDING_DIMENSION` cannot be per-tenant** and must stay a deployment fact:
the pgvector column is a fixed-width `vector(1024)`, so mixed dimensions cannot share the
index. `PYRRHULA_EMBEDDING_MODEL` may vary per tenant only among models of that width, and
changing either requires re-embedding every chunk. Documented as an exception rather than
quietly exempted.

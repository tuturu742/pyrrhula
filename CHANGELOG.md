# Changelog

Notable changes to Pyrrhula. Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning is [SemVer](https://semver.org/) with a `0.x` promise level: minor versions may
break APIs, the database always migrates forward.

## [Unreleased]

Everything since rc1. The through-line: several features were configurable in one place
and hardcoded in another, and this closes those gaps rather than adding new surface.

### Added

- **Per-persona and per-connection generation settings.** `temperature`, `seed`,
  `presence_penalty`, `reasoning_effort`, `num_ctx`, `max_tokens` and friends on a
  connection, overridable per persona. Precedence: request → persona → connection →
  platform default. This is what stops a cast sharing one model from converging into one
  voice. Travels in `.pyr`. See [docs/models.md](docs/models.md).
- **Per-server MCP limits.** `max_calls_per_session`, `timeout_seconds`,
  `max_result_chars` and a transport `options` bag, all on the registration — a timeout is
  a property of one server, and an external server cannot budget per session because it is
  never told which session is calling.
- **Scope bands travel in a bundle.** "Levels of lore" now export and import, with members
  carried as persona keys. Previously the knowledge arrived and the band did not, so a
  restricted source landed unreachable by everyone.
- **Configurable previews.** A preview runs what the project says it runs — a
  `pyrrhula-preview.json` in the repo or overrides on the repo row — instead of only ever
  serving a static site. See [docs/previews.md](docs/previews.md).
- **Retrieval models without the installer.** `PYRRHULA_SKIP_MODEL_DOWNLOAD=1`, plus
  Admin → Models: download from Hugging Face on demand, or upload a cache
  tarball for an air-gapped box.
- **An admin-console assistant.** Asks about the deployment and proposes config changes;
  it never applies them — you click Apply and it runs as you.
- **A settings resolution chain** (workspace → tenant → deployment default) behind
  `max_review_rounds` and `moderation_model`.
- **Reactive turn order** and a `chattiness` behaviour axis that actually schedules who
  speaks, rather than only styling how much they say.
- **Per-persona hosted-git identity**, so review and merge act as distinct bots.

### Fixed

- **Autonomous sessions ran to the step guard and stopped.** The interpreter reports
  "more to do" at its runaway-loop bound; the background task called it once and returned,
  leaving sessions `active` with nothing running and no fault shown. Resuming a session
  likewise did not restart its interpreter.
- **Every character rolled identical dice.** Actor stats came from a fixed stub that
  outlived the entity system by two phases, so the sheet a player was handed changed
  nothing.
- **The reranker was downloaded by every installer and never used** — configured,
  toggleable in the admin console, and connected to nothing.
- **Model-endpoint refusals.** Unsupported sampling parameters are dropped and retried,
  reasoning-vs-tools refusals are repaired, empty generations retry with a larger budget
  at the same reasoning level, and repairs chain when one refusal hides the next.
- **A connection setting `num_ctx` crashed every turn** on the streaming path and was
  silently ignored on the structured one.
- **Imports** adopt the bundle's `secret_mode` and conduct rules, heal secrets a first
  pass refused, and no longer duplicate personas on re-import.
- **AWS deployments could not produce a platform admin at all**, and baked in an assistant
  model with no credential.

### Changed

- Eight environment variables became settings, connection params or registration fields.
  `docs/configuration.md` is the reference, and CI now checks both that every variable is
  documented and that every documented default matches the code.
- Installers no longer require GitHub credentials, and a clean install assumes nothing
  about local models.
- **One randomizer instead of a tool per kind of randomness.** The `dice_roller` and
  `coin_flip` tools were the same handler over the same builtin, differing only in which
  rule system validated the roll — so there is now one tool, `randomizer`, and a call may
  name the system it resolves in. A coin is a `1d2` grammar with two outcome bands; an
  ungraded number is the stock `generic` system, which has a permissive grammar and no
  bands. Both are rule systems, which is where they always belonged. The MCP surface
  honours the same selection, which it previously did not: a bundle binding the tool to
  its own ruleset had its remote rolls validated against the stock d20 system instead.
- **The MCP deterministic-tool list is registry-driven, as documented.** It was keyed on
  one hardcoded tool name, so a pack registering a second deterministic tool got a
  surface that silently omitted it. It is keyed on the builtin now, and each tool
  resolves in the rule system its own definition names.
- **Domain words out of the core, and a lint that says so.** `dice_roller`,
  `dice_grammar`, `max_dice_count` and a `campaign_recap` report template had been sitting
  in `packages/core` against the project's own first rule, because the vocabulary check
  only ever scanned frontend display strings. They are now `randomizer`,
  `expression_grammar`, `max_term_count` and `narrative_recap`, and the check scans the
  backend too. Migrates forward; bundles and packs carry the new names.

## [0.1.0-rc1] — 2026-09-11

First public release candidate. Everything below is new, because everything is.

### The claim

Multi-agent conversations where **who-knows-what is enforced by the system, not requested
of the model**. A concealed secret's text is excluded from the model's context — a leak is
impossible by construction, not unlikely by prompting. See
[an annotated transcript](https://github.com/tuturu742/pyrrhula-samples/blob/master/hagnaryd-mystery/TRANSCRIPT.md)
of the engine holding a murderer's secret through a police interview.

### Engine

- **Multi-tenancy** — Postgres row-level security (`FORCE`) on every tenant-scoped table,
  enforced by a CI-blocking negative test suite that omits application filters and asserts
  zero rows.
- **Process engine** — declarative, versioned JSON flow DSL: phases, actors, per-phase
  visibility and token budgets, gates, awaits/timeouts; authored in a visual editor.
  Autonomous, directed (human-conducted), and managed modes.
- **Knowledge & retrieval** — versioned sources with per-class token budgets, weighted RRF
  fusion, cross-encoder rerank; every turn records a context manifest naming what was
  retrieved, from which version, at what rank.
- **Secrets** — first-class records with holders, gists, and behavioral directives. Three
  per-workspace trust modes: `excluded` (never in context), `trust` (the holder's own
  brief in its context, model judgement governs speech), `gate` (a per-turn structured
  classifier rules conceal / hint / reveal, enforced by exclusion, with a post-generation
  leak check). Gate model is tenant-configurable; failure fails closed.
- **Deterministic resolution** — dice and checks are seeded, code-executed, validated
  against the actor's own sheet via CEL modifier resolvers, hash-chained, and rendered
  from the record, never from prose. Ships a faithful Basic Fantasy RPG (CC-BY-SA)
  rule system alongside generic d20 and coin-flip systems.
- **Entities** — JSON Schema fields + CEL constraints + declarative state machines;
  semantic tags drive automatic sheet rendering. Characters and work items are the same
  object; domain content lives in packs, never in core.
- **Behavioral axes** — pack-defined dials (malice, cooperativeness, disclosure
  propensity, …) with prompt-directive and gate bindings; high-stakes axes feed the gate;
  sensible pack-declared defaults; append-only versioned profiles pinned per turn.
- **Overseer surfaces** — Director's View with live per-turn disclosure decisions; every
  overseer read of secret plaintext writes an audit row in the same transaction.
- **Portability** — `.pyr` workspace bundles (cast, briefs, knowledge, flows, secrets),
  export/import round-trip, optional encryption; API keys are never written to a bundle.
- **Workflows as plugins** — vocabulary overlays (RPG / enterprise / software-dev), packs
  as pure JSON from pinned plugin repositories; user-authored logic is schema + FSM + CEL
  only, never code.
- **Software-dev workflow** — delegation of work items to coding agents over MCP against
  real repositories, with server-side git, CI execution in isolated environments, and
  facilitator review loops.
- **Deployment** — one image (api / worker / migrate), verified installers for
  docker/podman compose and k8s; AWS ECS Terraform included. Fully-local operation with
  Ollama (no API keys), fully-cloud, or hybrid with per-purpose egress policy.
- **Auditability** — hash-chained append-only tables (audit log, session events,
  resolutions, disclosures), idempotency keys on every side-effecting operation,
  deterministic replay verified by CI.

### Samples

Four importable workspaces with step-by-step READMEs in
[pyrrhula-samples](https://github.com/tuturu742/pyrrhula-samples): a closed-house murder
mystery (six agents, five private briefs), a product-launch working session with
commercially confidential facts at the table, a two-agent game-dev loop, and Pyrrhula
working on its own codebase.

### Known limits, stated plainly

- The AWS ECS path ships as Terraform with a verified plan but has not had a recent
  live run; treat it as beta.
- Cheap models act imperfectly (a turn may slip voice); the engine's guarantees are
  about what a model *couldn't know*, and those hold regardless of model quality.
- Single-maintainer project; response times are honest, not instant.

[0.1.0-rc1]: https://github.com/tuturu742/pyrrhula/releases/tag/v0.1.0-rc1

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
- **Every installer now proves the deployment works, and fails if it does not.** The
  readiness gate answered "the API reached the database", which a stack whose worker
  OOMs on its first job also answers. The new check drives a document through the whole
  loop — a blob write, a job on the queue, the *worker* claiming and running it, a parse,
  rows in the database — and needs no workflow pack and no model, so it means the same
  thing on an install that will only ever run `swdev`. Re-runnable afterwards:
  `python /app/deploy-smoke.py` in the api container, or `deploy/aws/smoke.sh`.
- **The installer no longer downloads a retrieval model.** It was the slowest part of an
  install by a wide margin, spent on a model nobody had chosen. Choosing and fetching
  them is the operator's, in **Admin → Models**, which is where a platform admin now
  lands on first login while none is installed. Until then the deployment is installed
  and working; only semantic search waits.
- **The runtime never fetches a model mid-request.** It refuses one it does not have,
  with a message naming the page that fixes it. The lazy path cost minutes inside
  whichever turn happened to be first, and an unauthenticated hub check with no timeout
  wedged the API's event loop for thirteen minutes at idle CPU. `PYRRHULA_HF_OFFLINE`
  now defaults to `1` everywhere, and an admin-console download lifts it for that fetch
  alone.

- **Tenancy is an install-time flag.** `./install.sh <target> [--single-tenant |
  --multi-tenant]`, defaulting to single, on all three targets. It applies on every run
  rather than only at first install, so rerunning with the other flag is how a
  deployment switches — nothing is migrated either way, because single-tenant is a flag
  over the same multi-tenant core and always was.
- **The compose readiness gate no longer depends on a bug.** It sent a header-less login
  and accepted `401|403|404|422`; multi-tenant mode answers `400`, so choosing it made
  every install wait five minutes and then fail on a stack that was healthy throughout.
  It had only ever passed because single-tenant mode answered `404 unknown tenant:
  'dev'`. The probe now names a tenant slug nothing can own, so it is independent of the
  mode and still proves a real database lookup.
- **A single-tenant install needs no credentials.** Sign up and you are the one
  organization's owner *and* the platform admin — one account for both the workspace
  product and Admin → Models. Previously a solo operator got two accounts on their own
  machine and had to sign out of their own workspace, and in as a generated one, to
  choose an embedding model. Principals are per-tenant, so the role is derived rather
  than duplicated into a second identity with its own password to drift. It fails
  closed: the grant is scoped to the deployment's *sole* organization and to its owner,
  so a second organization removes it and a `viewer` who joins never had it. Multi-tenant
  is unchanged — there the platform admin is a different person by definition.
- **Single-tenant mode reaches the UI.** It was half a feature: the API stopped
  requiring an organization name and the sign-in form went on asking for one, because
  nothing told it the deployment's shape. A new unauthenticated `GET /auth/config`
  carries three booleans about that shape, and the pre-auth screens use them — the
  organization field is gone from sign-in, and a solo deployment that already has its
  organization offers "create your account" (joining it) instead of a create/join
  toggle and a slug to type. Creating a *second* organization is the one action that
  breaks single-tenant inference, so the UI stops steering people into it.
- **Single-tenant mode works.** It is on by default in compose and Kubernetes and had
  never been exercised: `PYRRHULA_DEFAULT_TENANT_SLUG` defaulted to `dev`, a tenant no
  installer creates, so a deployment that advertised "no organization field needed"
  answered every header-less login with `404 unknown tenant: 'dev'` — confirmed against
  a clean compose install. The slug is now inferred: with exactly one organization, that
  is the one. With several it refuses and says so rather than signing someone into the
  wrong one, and an explicit `X-Pyrrhula-Tenant` always wins.

- **Repository knowledge graphs build again.** Four separate faults stood between a
  workspace with repositories attached and a graph. One binary file failed the whole
  analysis — `read_tree` documented that it skipped binaries and did not, because
  `git show` on a blob *succeeds* and the `UnicodeDecodeError` escaped the handler that
  only caught `GitStoreError`. The failure was then invisible: the page had no concept
  of job status, so a job that failed looked exactly like one nobody had asked for, and
  a refresh lost even the "queued" notice. Both fixed, and the page now reports what the
  last run did.
- **Repositories can be refreshed.** Import happened once at registration and nothing
  ever re-read the source, so a repository registered from a remote diverged silently
  and for ever — a knowledge graph went on describing the code as it stood on
  registration day. `POST /repos/{id}/refresh` fast-forwards from the source. Never
  force, never a reset: the store is where approved pull requests land, so a refresh
  that could rewind is not a refresh. When both sides have moved it reports the
  divergence and changes nothing.
- **The working branch is a property of a repository, not the constant `main`.** The
  store assumed `main` in fourteen places and renamed imported heads to match, which
  made it internally tidy and destroyed the one fact every outbound operation needs: on
  a live deployment, two of three repositories used `master` and were silently dropped
  from analysis, while delegated coding would have degraded to scaffolding with nothing
  reporting why. The remote's own name is read from the remote (`ls-remote --symref`,
  so it works for any host and cannot disagree with a fetch) and stored on the
  repository, where it can also be pointed at a release-candidate branch to put agents
  on stabilisation work.

- **Delegated agents can delete files.** The codegen protocol could only write, and
  both commit paths write, so a file the model omitted stayed where it was: "remove the
  dead module" committed nothing and opened an empty pull request with no explanation.
  `===DELETE: path===` joins `===FILE:`, carried through the in-container and
  store-side commits alike (`git add -A` already staged removals; nothing was removing
  anything). Deletion is a verb rather than an inferred absence, because omitting a file
  is how a model says "I did not need to touch this". Paths are confined to the
  checkout, checked again at the point of removal rather than trusted from the parser,
  and a write wins over a delete for the same path.
- **A delegation that produces no applicable change opens an honest pull request**
  instead of dying. The in-container path had always allowed an empty commit and the
  store-side path had not, so it failed there with git's bare empty error.

- **A delegated agent's assignee is chosen, not indexed.** An unassigned work item fell
  to `devs[index % len(devs)]`, so the ordering of persona ids decided who built what —
  which makes a tiered roster a queue. The session's facilitator is asked instead, with
  the work item and the roster, and its reasoning is logged and posted to the session;
  the round robin survives only as the fallback when no answer comes back. The mechanism
  for a supervisor to name an assignee had always existed — nothing ever asked one.
- **A merged pull request moves its work item to `merged`.** The lifecycle had the
  `approved --merge--> merged` transition and nothing drove it, so a landed branch left
  its item at `approved` for ever: the repository said merged, the board said not, and
  the board is what a human reads.
- **Codegen no longer pins a temperature.** A hardcoded `0.2` is a preference on models
  that accept it and a hard failure on those that do not — Anthropic's refuse anything
  but `1` — so every delegation assigned to such a persona raised `UnsupportedParamsError`
  and fell back to writing a placeholder file named after the work item. A reviewer duly
  rejected a pull request whose real fault was three layers up.

### Security

- **A repository's access token was stored in plaintext on the volume.** `clone_from`
  passes the token as userinfo on the clone URL, and `git clone` persists the URL it is
  given as `origin` — so every imported repository kept a live, writable credential in
  `.git/config`, readable by anything that could reach the blobs volume, surviving
  container recreates and riding along in backups. Found on a live deployment where all
  five repositories held one. The docstring had claimed "never stored, never logged";
  the second half was true and carefully done, the first was simply wrong about what git
  does. The remote is now rewritten to the bare URL immediately after cloning, and it
  raises rather than leaving a credential quietly in place. **Anyone who registered a
  repository with a token before this should rotate it.**

### Removed

- `docs/deploy-verification.md` and its scripts, along with the demo and benchmark
  drivers. They provisioned specific showcase tenants against a live stack with specific
  local models — one person's harness, not something a self-hoster could run — and the
  question they were nominally for ("did this install work?") is the installer's own
  now. A pack's mechanics stay covered by `tests/packs/` and `tests/isolation/`, in CI,
  without a live stack.
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

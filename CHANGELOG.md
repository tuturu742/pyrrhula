# Changelog

Notable changes to Pyrrhula. Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning is [SemVer](https://semver.org/) with a `0.x` promise level: minor versions may
break APIs, the database always migrates forward.

## [Unreleased]

### Fixed

- **Stopping one preview stopped others.** A repository's preview of "the latest build"
  is named so that it is a prefix of its branch previews' names, and stopping or expiring
  it tore those down too (every engine for stops; Kubernetes also on redeploy). Stops now
  match the one preview exactly.
- **Markdown showed as markup.** Session transcripts, both assistants, the session agenda
  and the repository graph now render Markdown (bold, lists, code, links, tables) instead
  of showing `**` and backticks. Raw HTML in model output stays text.
- **"Play the build" hid a running preview** behind an older stopped one for the latest
  build. Until you pick, the row now shows the preview that is running.
- **The assistant gave up on long requests.** A request that needed many reads ended in
  "tool loop exceeded" with nothing proposed. It is now warned two rounds before its
  budget ends, its last round can only propose, and a turn that proposed something ends
  normally.

### Changed

- **The assistant will not propose a secret whose gist gives it away.** The gist is the
  part others may see; a proposal whose gist repeats the secret is refused and the model
  is asked for one that says what the secret is about instead.

## [0.1.0] - 2026-10-06

The first public release: a self-hosted, multi-tenant platform for orchestrating teams of
AI agents and people, where who knows what is enforced by the system rather than requested
of the model. Software development, facilitated team workflows and tabletop RPG run on one
domain-neutral core.

### Core

- **Tenancy.** Every tenant-scoped table carries row-level security with `FORCE`;
  database sessions set the tenant transaction-locally and fail safe to zero rows. A CI
  suite runs without the application's filters and asserts nothing leaks. One install
  serves many organizations, or one (`--multi-tenant` is a flag, not a build).
- **Process engine.** Sessions follow a declarative, versioned flow: phases, who acts in
  each, per-phase visibility and token budgets, requirement gates, awaits and timeouts —
  edited in a visual flow editor. Sessions run autonomously or are conducted turn by turn.
- **Knowledge and retrieval.** Sources in three classes (rules, lore, misc) with
  per-phase budgets; scopes decide who may read each entry, applied as a SQL predicate at
  retrieval time ("levels of lore"). Hybrid dense and lexical search, weighted fusion and a
  cross-encoder rerank, on self-hosted models the operator chooses (`bge-m3` and
  `bge-reranker-v2-m3` suggested).
- **Context assembler.** One code path from stored text to a model's context, taking the
  viewer and the phase as required arguments; every turn records a manifest of what was
  retrieved, from which version, at what rank and why, readable in the context inspector.
- **Secrets.** First-class secrets with holders. A concealed secret is excluded from the
  context, not hidden by instruction; a disclosure gate decides conceal, hint or reveal on
  gists and fails closed; a post-generation leak check runs after every turn; per-workspace
  trust modes; an overseer view whose every plaintext look is an audit row.
- **Deterministic results.** Dice, coins and checks are seeded, executed in code,
  validated against the entity record with CEL, hash-chained, and rendered from the record.
- **Entities.** JSON-Schema records with derived values, views and state machines —
  character sheets, work items, pull requests, builds — with change history.
- **Behavioural axes** per persona, with engine-enforced limits on the high-stakes ones.
- **Reports.** Recap, session log, decision summary, composed document and full
  transcript, as Markdown and PDF.
- **Portability.** `.pyr` bundles carry a workspace — personas, knowledge, flows,
  entities, secrets under one of three export modes, images pinned by digest — with
  optional password encryption; character cards in and out.
- **Vocabulary overlays.** The core speaks neutral terms; Default, RPG and
  software-development overlays label the UI, per workspace or per organization.
- **Metering and limits.** Every model call is metered by purpose; daily token caps per
  organization, connection, persona and user; an egress policy on the model port.

### Software development

- **Delegated coding work.** A facilitator turns an agenda into work items and delegates
  them; coding agents work through a harness in isolated containers — sibling containers
  on the engine socket, Kubernetes jobs, or a Docker host managed in Portainer — run the
  tests and open a pull request on your repository; the facilitator reviews it under a
  second identity and sends it back or approves it; a batch gets a merge order.
- **Repositories** on GitHub, GitLab, Gitea or any git remote, mirrored in a hosted store;
  **Analyze repos** writes what they achieve into workspace knowledge as a graph.
- **Previews.** A green build runs behind a share link that expires; per-repository
  recipes cover servers and terminal programs as well as static builds.
- **Images.** Toolchain images are built on builders the operator declares (GitHub
  Actions, Portainer, a webhook to your CI), verified by digest and smoke-tested on the
  tenant's engine before anything runs in them; registries and an allowlist.
- **No provider key enters a container.** Agents call models through an inference proxy
  with a short-lived token scoped to one connection.

### The assistant

- A chat on every workspace page that reads the workspace under the asking user's own
  permissions (secrets as gists only), answers from the shipped documentation with a
  citation, and can propose any edit the UI can make — applied only by the user's click,
  with the user's own rights. The admin console has a smaller sibling for the deployment.

### Tools and models

- Agent web search through the bundled SearXNG, page fetch, and a ledger of every call a
  session made; MCP servers per workspace behind an allowlist with per-session caps.
- Any provider LiteLLM speaks, including a local Ollama; per-persona sampling; parameters
  a provider refuses are repaired rather than failing the turn.

### Install and operations

- **Published images** — `ghcr.io/tuturu742/pyrrhula` and `ghcr.io/tuturu742/pyrrhula-web`
  (linux/amd64) — with a one-command installer that generates secrets, migrates, runs an
  end-to-end check and prints the next steps. From a checkout: `./install.sh compose` or
  `./install.sh k8s`; a Portainer stack; HTTPS with a self-signed or your own certificate.
- An admin console for organizations, plugin repositories, registries, builders, the
  retrieval models and the assistant.
- Workflow packs come from plugin repositories pinned by commit (`pyrrhula-workflows`:
  `rpg`, `swdev`) beside the built-in Default; packs are content, never code.
- Nine sample workspaces in [pyrrhula-samples](https://github.com/tuturu742/pyrrhula-samples),
  each a `.pyr` with a step-by-step README.

### Known limitations

- Images are linux/amd64 only; on Apple Silicon they run under emulation.
- The retrieval models (~6.5 GB for the defaults) are downloaded after install, from
  **App settings → Models**; until then sessions run without semantic search.
- Small local models hold a role unevenly in sessions with many actors.
- The AWS ECS execution engine is experimental and unverified; OIDC/SAML login is not
  there yet.

[Unreleased]: https://github.com/tuturu742/pyrrhula/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/tuturu742/pyrrhula/releases/tag/v0.1.0

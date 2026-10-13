# Changelog

Notable changes to Pyrrhula. Format follows [Keep a Changelog](https://keepachangelog.com/);
versioning is [SemVer](https://semver.org/) with a `0.x` promise level: minor versions may
break APIs, the database always migrates forward.

## [Unreleased]

## [0.2.2] - 2026-10-14

A turn speaks only for its author. See
[docs/models.md](docs/models.md#when-a-model-speaks-for-someone-else).

### Fixed

- **A model could write other participants' lines.** Shown a multi-party transcript, a
  model sometimes went on past its own turn and wrote the next speakers' lines. They were
  saved as its message, replayed to everyone, and taken by those participants as their
  own words: a player defended "its opening", which another player's model had written.
  A live turn is now cut before the first block that is another participant's speech.
  That means first-person blocks for two or more others, or a block introducing itself
  as someone else. Each cut is recorded as a warning on the session. Addressing someone
  ("Reese: walk me through your case") and list items are left alone. Replayed over 6,382
  replies from Mafia games, the rule cut 5, all of them scripts.
- **Raising a hand made the model write again.** After the tool result, models repeated
  their speech or drifted into the next speakers' lines. A tool result can now end the
  turn (`ToolResult.end_turn`), and `raise_hand` does, once the turn has said something.
- **`raise_hand` was offered with no turns left.** Told a raise would not be called, a
  model deliberated over the tool in its message and gave its role away. A speaker who
  has used up `max_turns_each` is no longer offered the tool.

## [0.2.1] - 2026-10-13

An open floor for discussions. See
[docs/participants-and-ballots.md](docs/participants-and-ballots.md#an-open-floor-raised-hands).

### Added

- **`order: hands`**, a turn order for discussions instead of speech rounds. Everyone
  speaks once in seat order. Then the floor goes to whoever the last message named, so an
  accused participant answers straight away. Otherwise it goes to the hand raised longest
  ago, until nobody wants it. A speaker raises a hand with the `raise_hand` tool, which
  says how many turns they have left; not raising it is how a speaker passes.
  `max_turns_each` caps one participant's turns in the phase. `answer_named: false`
  leaves the floor to raised hands alone.
- Raised hands are recorded per turn in a new append-only table, `floor_request`, so who
  spoke when is decided from the record, never parsed from a model's words.

### Upgrading

One migration: the `floor_request` table (append-only, with tenant RLS).

## [0.2.0] - 2026-10-13

Flows with participants: per-persona values dealt in secret, sealed votes counted by the
engine, private checks and private channels. Built for hidden-role games (the Mafia sample
in `pyrrhula-samples`) from parts that know nothing about games. Also, the
usage records now carry what providers really count. See
[docs/participants-and-ballots.md](docs/participants-and-ballots.md).

### Added

- **Flow settings.** A flow declares typed, ranged values (a seat count, a discussion
  length) that a session overrides at creation; any count in the flow can read one.
- **Participants.** Per-participant variables, dealt by a recorded draw at session start.
  A deal can be narrowed by CEL to deal a role inside a team. Hidden variables reach only
  their holder, the supervisor and, with a team variable, their team. They are excluded
  from everyone else's context, history and recaps, not withheld by instruction.
- **Seat-order turns.** `order: seated` walks the participants in seat order for a number
  of laps, rotating the first seat each time, filtered by a CEL `where` re-checked at
  every turn.
- **Audiences.** A phase's messages reach only the participants its `members` expression
  admits: a private channel.
- **Sealed ballots.** A phase can be a vote: one ballot per actor through a `ballot` tool,
  counted when the phase ends, with effects on the chosen participant in the same
  transaction. Ties choose nobody or are drawn. Ties can mark their candidates for a
  run-off. Results become public as every vote, as the outcome only, or not at all.
- **Private checks.** `reveal: voters` with `disclose`: the engine computes facts about the
  chosen participant from the record, and only the voters read them. The supervisor does
  not see them either.
- **Phase effects on participants.** Set a variable on every participant a CEL filter
  admits when a phase ends, before its vote closes.
- **Phase markers and recaps.** A phase can name its moment ("Night 2"), and markers can be
  placed in the replayed history. A phase can also be recapped per viewer from the record.
  When history is trimmed, the recaps of dropped phases stay. Sessions switch both with
  `options`. In the Mafia runs, markers cut phase and alive-status confusion from 6.2% to
  0.8%. Recaps are off by default: they showed no measurable gain.
- **Provider-reported usage.** Token counts come from the provider when it reports them,
  hidden reasoning included. A usage row says whether its counts are reported or
  estimated.
- **Refused settings are recorded.** A connection or persona setting that a provider
  refuses is still dropped so the turn goes through, and the session now records a
  warning, shown beside the seat.

### Changed

- OpenRouter calls name Pyrrhula (`X-Title`, and the deployment's public URL as referer),
  so they are credited to Pyrrhula in the OpenRouter console instead of to LiteLLM.
- A required vote that a model answers in words is asked for once more within the same
  turn, and only a successful call counts as the move.
- Importing a bundle keeps a flow's versions under one flow instead of forking each.
- Tool-call arguments are validated against the tool's JSON Schema before it runs;
  `jsonschema` is now a declared dependency.

### Fixed

- **A turn that failed in an unexpected way left its session "active" with nothing
  running.** Any turn failure now pauses the session with the error, so a resume
  retries it.
- **Gemini's direct API broke turns with tool calls.** Its thought signature in the call
  id overflowed the idempotency key. Long ids are now digested.
- **A silent stream or an unanswered request waited forever.** The turn's time budget now
  covers every wait.
- **An unknown tool name killed the turn.** It is now answered with an error result the
  model can act on. Words said alongside a tool call are kept in the transcript.
- **Leak-check regenerations failed on sealed connections** (OpenRouter among them) and
  were not metered. They now use the connection's key and record their usage.
- **Empty messages broke Cohere** (and any endpoint that refuses them). They no longer
  reach a request. An empty reply to an instruction placed last is asked again as the user.
- **A vote result showed hidden changes to everyone.** A change to a hidden variable now
  reaches only the supervisor, the participant changed and the voters of a private vote.

### Upgrading

Three migrations: usage rows gain reported/reasoning token columns, sessions gain
`options`, and participants, ballots and audiences get their tables (append-only, with
tenant RLS). They apply the same way as every upgrade's migrations.

## [0.1.1] - 2026-10-06

Fixes found while preparing the launch material, and a release process that lets a fix ship
while the next feature set is still being built.

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
- **Releases are one command and two lines.** `scripts/set_version.py` sets every version
  string a release needs; CI also runs on `release/X.Y` branches so patch releases can be
  tagged there; only the highest final version is marked *latest*, which is what the
  one-line installer follows. See [docs/releasing.md](docs/releasing.md).

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

[Unreleased]: https://github.com/tuturu742/pyrrhula/compare/v0.2.2...HEAD
[0.2.2]: https://github.com/tuturu742/pyrrhula/releases/tag/v0.2.2
[0.2.1]: https://github.com/tuturu742/pyrrhula/releases/tag/v0.2.1
[0.2.0]: https://github.com/tuturu742/pyrrhula/releases/tag/v0.2.0
[0.1.1]: https://github.com/tuturu742/pyrrhula/releases/tag/v0.1.1
[0.1.0]: https://github.com/tuturu742/pyrrhula/releases/tag/v0.1.0

# Pyrrhula

[![CI](https://github.com/tuturu742/pyrrhula/actions/workflows/ci.yml/badge.svg)](https://github.com/tuturu742/pyrrhula/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)

**A multi-tenant platform for structured, auditable, asymmetric-knowledge multi-agent
conversations — where who-knows-what is enforced by the system, not requested of the model.**

Pyrrhula runs structured conversations between AI agents and humans. Knowledge, process
rules, and participant state are versioned, structured data rather than prompt text, and the
conversation is driven by an explicit, user-configurable phase/turn engine rather than
free-form chat.

The first target use case is **AI-managed tabletop RPG campaigns**. The core engine is
domain-neutral, so two further use cases run on the same engine with different vocabulary
overlays and content packs: **structured enterprise multi-agent workflows** (multi-perspective
strategy discussions and planning simulations, sequential review/approval loops) and
**multi-agent software development**, where a facilitator agent acts as an
engineering manager proposing tasks and participant agents act as engineers on a shared
repository, with implementation delegated to external coding agents over MCP. The stated end
state of the third use case is dogfooding: Pyrrhula's own backlog worked by a team of agents
managed by Pyrrhula. One product, one codebase.

> Status: **approaching 0.1.0-rc.** The full stack described below is implemented and
> running: tenancy/RLS, knowledge & retrieval, the process engine, the context assembler,
> deterministic resolution, the secrets layer (disclosure gate, structural exclusion,
> post-generation leak check, per-workspace trust modes), the overseer's Director's View,
> behavioral axes with engine-enforced high-stakes dials, portable `.pyr` workspace
> bundles, and two verified install paths (compose, k8s). Seven runnable
> sample workspaces live in
> [pyrrhula-samples](https://github.com/tuturu742/pyrrhula-samples) — start with the
> murder mystery, or [read a finished session first](https://github.com/tuturu742/pyrrhula-samples/blob/main/hagnaryd-mystery/TRANSCRIPT.md)
> to see the disclosure gate working before you install anything.

## Install

```bash
./install.sh compose   # docker or podman on this machine
./install.sh k8s       # a Kubernetes cluster (one-command dev install on k3s)
```

Each installer checks prerequisites (`--check` to only check), generates secrets,
brings the stack up, and prints the URL — then you sign up in the browser and the
setup checklist takes over — a built-in general discussion workflow ("Default") works
out of the box; specialized workflows (RPG, software development) come from plugin
repositories, with the official one preinstalled. Full walkthrough, upgrade paths, and troubleshooting:
[docs/install.md](docs/install.md).

## Why another AI chat tool?

Tools like SillyTavern proved the demand but share structural limits: knowledge is prompt
blobs, turn order is ad hoc, secrets survive only as long as the model feels like keeping
them, and everything is single-user. Orchestration frameworks (LangGraph, AutoGen, CrewAI)
have real graph execution but no visibility model at all — every agent is trusted and every
developer is the only tenant. Nobody has built the intersection. Pyrrhula's defensible claim:

- **The GM can know things you don't — enforceably.** Visibility is scoped per role and per
  phase, at retrieval time, in the database.
- **An NPC can hold a secret it *structurally cannot* blurt out.** A concealed secret's text
  is removed from the model's context entirely; the agent acts on an author-written
  behavioral directive instead. A leak isn't unlikely — it's impossible by construction.
- **Dice never lie.** Rolls are seeded, code-executed, validated against the actual character
  sheet, hash-chained, and rendered in the UI from the database record — never from model
  prose.
- **Every ruling is explainable.** Each turn records a context manifest: what was retrieved,
  from which rulebook version, at what rank, and why. "Why did the Arbiter rule that way?" is
  answerable six months later.

## Core concepts

The schema uses domain-neutral vocabulary; the UI relabels it through a per-workspace
**vocabulary overlay**:

| Core term | RPG overlay | Enterprise overlay | swdev overlay |
|---|---|---|---|
| Workspace | World / Campaign | Workspace | Project |
| Process Definition | Session Flow / Turn Structure | Workflow | Engineering Workflow |
| Facilitator Agent | Arbiter | Facilitator / Chair | Engineering Manager |
| Participant Agent | PC / NPC bot | Domain Expert Agent | Engineer |
| Knowledge Source (rules/lore/misc) | Rulebook / Lorebook / Miscellany | Policy Doc / Domain Context / Reference | Engineering Standards / Business Context / Reference |
| Entity + Entity Schema | Character + Sheet Template | Ticket / Project + Record Type | Work Item + Item Template |
| Deterministic Tool | Dice Roller / Stat Calculator | Calculator / Policy Lookup | Checklist Runner / Estimator |
| Secret | Hidden Motive | Confidential Info / MNPI | Embargoed Info |
| Overseer | Director / Table Owner | Compliance Reviewer | Engineering Director |

What belongs in `rules` vs `lore` vs `misc` — and why the split drives retrieval
budgets — is spelled out per workflow in [docs/knowledge-classes.md](docs/knowledge-classes.md).

Key mechanisms:

- **Process Definition engine** — a custom interpreter over a declarative, versioned JSON DSL
  (phases, actors, per-phase visibility and token budgets, gates, awaits/timeouts for
  play-by-post pacing). Authored in a visual editor by non-programmers.
- **Priority-weighted retrieval** — rule-vs-lore priority is a *budget allocation* (per-phase
  token quotas per knowledge class, weighted RRF fusion, cross-encoder rerank), never a score
  multiplier. Deterministic, tunable, auditable.
- **Context Assembler** — the single code path from stored text to model context. Takes the
  viewing principal and the phase as required arguments; enforces scopes, budgets, secret
  exclusion, and emits the manifest.
- **Secrets & the disclosure gate** — a first-class `Secret` record with holders and a
  disclosure state machine. A cheap structured pre-decision (on gists, never plaintext)
  chooses conceal / hint / reveal; concealment means context *exclusion*, with a
  post-generation leak check as defence in depth. A human overseer can always inspect —
  and every inspection writes an audit row in the same transaction.
- **Behavioral parameters** — structured axes (talkativeness, cooperativeness, secret
  disclosure propensity, …) with per-axis bindings; high-stakes axes are enforced by the
  engine and gated on a CI eval harness that measures leak rates per provider.
- **Generic entity framework** — JSON Schema fields + CEL expressions + declarative state
  machines; semantic tags (`resource`, `status_set`, `progression`, …) drive automatic sheet
  rendering. Fantasy characters and support tickets are the same object. RPG content ships
  as a pack, never in the core.
- **Multi-tenancy from day one** — Postgres row-level security (`FORCE`) on every
  tenant-scoped table, backed by a CI-blocking negative test suite that deliberately omits
  application filters and asserts zero rows.

## Technology stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Pydantic v2, celpy |
| Database | PostgreSQL 16 only — relational + pgvector + JSONB + tsvector + job queue |
| Cache / fan-out | Redis (SSE fan-out, rate limits, retrieval cache) |
| Embeddings / rerank | bge-m3 (1024-dim) + bge-reranker-v2-m3, self-hosted ([swappable](#the-bundled-models)) |
| Model providers | LiteLLM behind a `ModelProvider` port — Ollama, OpenAI, Anthropic, Gemini, … |
| Frontend | React 18 + Vite + TypeScript, React Flow, Tailwind + shadcn/ui, TanStack Query |
| Streaming | SSE (POST for commands), Redis pub/sub across workers |
| Deployment | One image, entrypoint selects api / worker / migrate; compose or k8s via `install.sh` |

Three deployment modes are supported: **full local** (Ollama only, no API keys), **full
cloud**, and **hybrid** with a per-tenant egress policy deciding which purposes
(generation, gate, rerank, embed, report, rewrite) may reach hosted providers.

### The bundled models

Two models are fetched from Hugging Face when the platform admin chooses them under
**Admin → Models**, and run in-process. Pyrrhula does **not** redistribute them — your deployment fetches them, so their licences bind you
directly rather than through us.

| Purpose | Model | Licence |
|---|---|---|
| Embeddings | [`BAAI/bge-m3`](https://huggingface.co/BAAI/bge-m3) | MIT |
| Reranking | [`BAAI/bge-reranker-v2-m3`](https://huggingface.co/BAAI/bge-reranker-v2-m3) | Apache-2.0 |

Both are permissive and carry no field-of-use restriction, so a commercial deployment
needs no additional grant. Nothing else is fetched from Hugging Face at runtime.

**Both are swappable.** They are configuration, not architecture — retrieval reaches them
through the `EmbeddingProvider` and `Reranker` ports, and the platform admin picks them
under **Admin → Models** (any sentence-transformers model; the reranker can be disabled
outright, which leaves WRRF order untouched). The declared vector width is asserted
against the loaded model at startup, so a mismatched swap fails loudly on boot instead
of quietly returning nothing at query time.

Two things to know before changing the embedding model. Existing vectors are **not**
re-embedded: a swap orphans every stored chunk embedding, so re-index or start clean.
And the deployment always loads models offline (`HF_HUB_OFFLINE=1`), because a cold
in-request download blocks the first knowledge call for minutes; the only fetch path is
the download (or cache upload) on **Admin → Models**.

## Status and roadmap

The core is built and verified: the walking skeleton, the core loop, the
secrets/overseer/disclosure gate, three workflow packs on an unchanged core, and
portability (`.pyr` round-trip, CCv3 cards, sanitised reports, delegated coding work).
What comes next is in [ROADMAP.md](ROADMAP.md).

## Repository guide

| Path | What it is |
|---|---|
| `README.md` | This overview |
| `CLAUDE.md` | Ground rules for coding agents working in this repo |
| `docs/agent-guide.md` | Detailed definitions, conventions, and architecture reference for implementers |
| `CHANGELOG.md` | What changed, release by release |

Running one:

| Document | What it covers |
|---|---|
| [`docs/install.md`](docs/install.md) | Getting a deployment up, and the retrieval models |
| [`docs/self-host.md`](docs/self-host.md) | The manual compose path, TLS, the admin account and assistant |
| [`docs/configuration.md`](docs/configuration.md) | Every environment variable, how a setting resolves, and what deliberately is not an env var |
| [`docs/models.md`](docs/models.md) | Model connections, per-persona sampling, and what happens when a provider refuses something |
| [`docs/knowledge-classes.md`](docs/knowledge-classes.md) | Knowledge classes, scopes, and retrieval budgets |
| [`docs/entities-and-state-machines.md`](docs/entities-and-state-machines.md) | Entity schemas, the optional state machines on them, and attaching one to a persona |
| [`docs/mcp.md`](docs/mcp.md) | Attaching external tools, and the per-server limits |
| [`docs/exec-engines.md`](docs/exec-engines.md) | Where delegated coding work builds and tests |
| [`docs/previews.md`](docs/previews.md) | Running a build where a human can open it |
| [`docs/portability.md`](docs/portability.md) | `.pyr` bundles: what travels, the export modes, and what import will not overwrite |
| [`docs/packs-and-samples.md`](docs/packs-and-samples.md) | Registering workflows from `pyrrhula-workflows`, and setting tenants up from `pyrrhula-samples` |
| [`docs/operations.md`](docs/operations.md) | Operator tasks on a running deployment: deleting a tenant, resetting a password, the admin console, troubleshooting |

Where this README and the code disagree, the code is what runs — and the disagreement is a
bug in one of them.

## Licence

Pyrrhula is free software under the **GNU Affero General Public License v3.0 only**
([LICENSE](LICENSE)). Run it, study it, change it, share it.

The Affero clause is the part that matters here: if you modify Pyrrhula and let other
people use it **over a network**, you have to offer them the source of your modified
version. An ordinary GPL would not require that, and this is a product people run as a
service.

Two things that are deliberately *not* AGPL, because they are content you are meant to
adapt rather than code you are meant to extend:

| Repository | Licence |
|---|---|
| Pyrrhula (this repository) | AGPL-3.0-only |
| [pyrrhula-samples](https://github.com/tuturu742/pyrrhula-samples) — importable example workspaces | MIT |
| [pyrrhula-workflows](https://github.com/tuturu742/pyrrhula-workflows) — workflow packs (schemas, flows, axes) | MIT |

So a `.pyr` bundle you build from a sample, or a workflow pack you write starting from
one of ours, carries no obligation back to us. Only the engine does.

Copyright © 2026 tuturu742.

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md), which explains
the contributor agreement. Security reports go through
[SECURITY.md](SECURITY.md), never a public issue.

## Name

*Pyrrhula* is the genus of the Eurasian bullfinch. No product of that name exists in the
AI-agent or tabletop space; a formal trademark search is still pending.

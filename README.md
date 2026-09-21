# Pyrrhula

Pyrrhula is a self-hosted, multi-tenant orchestration engine for structured groups of
humans and AI agents.

Its central guarantee is simple: **who knows what is enforced by the system, not requested
of the model**. A concealed fact is removed from an agent's context instead of being
included with an instruction to keep quiet. Visibility decisions, deterministic tool
results, and generated turns are recorded so the resulting session can be inspected and
replayed.

Pyrrhula supports collaborative workflows such as tabletop sessions, investigations,
reviews, planning exercises, and structured deliberation without baking any one domain's
vocabulary into the core.

## What it provides

- **Multi-agent process execution.** Declarative phases, turn scheduling, awaits,
  checkpoints, resumable sessions, and human participation.
- **Enforced information asymmetry.** Knowledge scopes, per-agent visibility, secrets,
  disclosure decisions, and post-generation leak checks.
- **Hybrid retrieval.** Sparse and dense retrieval, reranking, activation rules, token
  budgets, citations, and context manifests.
- **Deterministic tools and rule systems.** Validated calculations and policy lookups
  whose inputs and outputs can be audited.
- **Structured entities.** JSON-defined schemas, finite-state machines, semantic tags,
  validated mutation, and generated sheets.
- **Portability.** Native `.pyr` import/export and Character Card V3 interoperability.
- **Self-hosting.** Docker Compose, Kubernetes, and AWS ECS deployment paths.
- **Provider choice.** Hosted models through LiteLLM or local generation through Ollama.
- **Extensible workflow packs.** Domain content remains declarative and separate from the
  domain-neutral engine.
- **Agent delegation.** Coding agents can work in isolated shell, Docker, Kubernetes, or
  ECS execution environments.

## Why exclusion matters

Prompting a model not to reveal a secret is not a security boundary. If plaintext reaches
the generation context, it can be repeated, transformed, inferred from, logged, or exposed
by a provider.

Pyrrhula assembles a separate context for every turn. Knowledge, entities, history, and
secret disclosures are resolved for the current participant before generation. Concealed
plaintext is omitted. The resulting context manifest records what was included and why.

Workspaces can choose among three secret modes:

- **Excluded** — held secrets never enter generation context.
- **Trust** — a participant's own secrets may enter its context with their directives.
- **Gate** — a structured classifier decides conceal, hint, or reveal from topical gists;
  it does not receive the secret text and fails closed.

No mode gives one participant another participant's secret.

## Quick start

The supported installation entry point is `install.sh`.

### Docker Compose

```bash
./install.sh compose
```

The installer checks prerequisites, starts Postgres, Redis, the API, worker, web
application, and supporting services, applies migrations, and runs a smoke check.

See [docs/install.md](docs/install.md) and
[docs/self-host.md](docs/self-host.md) for configuration, TLS, upgrades, backups, and
operational guidance.

### Kubernetes

```bash
./install.sh k8s
```

The Kubernetes deployment uses the manifests under `deploy/k8s/`. Development overlays,
TLS resources, execution-engine RBAC, and optional host Ollama integration are provided.

### AWS

```bash
./install.sh aws
```

The AWS deployment uses ECS Fargate, RDS Postgres, ElastiCache Redis, EFS, ECR, Secrets
Manager, and an Application Load Balancer. Read `deploy/aws/README.md` before applying:
the stack has a real monthly cost.

## Local development

Pyrrhula requires Python 3.12. Python dependencies and commands are managed with
[uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra dev
uv run alembic upgrade head
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Postgres and Redis are required for the full suite. Tests that need unavailable services
may skip locally, so inspect the test summary rather than treating any green run as proof
that integration behaviour ran.

Workflow-pack tests also require the repositories pinned in `deploy/plugins.json`:

```bash
python scripts/fetch_plugins.py
```

### Web application

The web application is a Vite/React project managed with pnpm.

```bash
cd web
pnpm install --frozen-lockfile
pnpm test
pnpm build
```

## Architecture

The Python application is split into four principal layers:

1. **Core** contains domain-neutral models and services.
2. **Ports** define infrastructure and provider boundaries.
3. **Adapters** implement those ports for Postgres, local storage, cloud services,
   execution environments, model providers, and other integrations.
4. **API and worker composition roots** connect transports and background jobs to core
   services.

Workflow packs carry vocabulary and declarative domain content. The engine does not
contain special cases for campaigns, tickets, mysteries, pull requests, dice, or any
other individual use case.

The web application consumes the HTTP and streaming APIs. Postgres row-level security is
the final tenant-isolation boundary; Redis supports queues, rate limiting, and streaming
coordination.

## Repository map

| Path | Purpose |
|---|---|
| `packages/core/` | Domain-neutral models, services, ports, and invariants |
| `packages/adapters/` | Infrastructure and provider implementations |
| `packages/api/` | HTTP API, authentication, middleware, routes, and MCP server |
| `packages/worker/` | Background jobs and delegated work |
| `packages/eval/` | Evaluation scenarios, runners, metrics, and reports |
| `web/` | React web application |
| `builtin-workflows/` | Built-in declarative workflow content |
| `migrations/` | Alembic migrations and SQL baseline |
| `tests/architecture/` | Import-boundary and architecture checks |
| `tests/isolation/` | Tenant-isolation and RLS negative tests |
| `tests/leak/` | Secret-exclusion and leak tests |
| `tests/replay/` | Replay and context-manifest tests |
| `tests/packs/` | Workflow-pack contract tests |
| `deploy/` | Compose, Kubernetes, AWS, installers, and MCP sidecars |
| `docker/` | Container images and self-hosted Compose definitions |
| `docs/` | User, operator, and coding-agent documentation |
| `scripts/` | CI, documentation, plugin, and sample utilities |
| `CLAUDE.md` | Hard rules for coding agents |
| `CONTRIBUTING.md` | Contribution workflow and required checks |
| `ROADMAP.md` | Project direction |
| `CHANGELOG.md` | Release history |

## Configuration

Environment variables and their defaults are documented in
[docs/configuration.md](docs/configuration.md). CI verifies that documented environment
variables match the variables read by the code.

Model connections, per-persona generation parameters, workspace settings, MCP servers,
preview recipes, and execution engines are configured separately from deployment
environment variables. See:

- [docs/models.md](docs/models.md)
- [docs/mcp.md](docs/mcp.md)
- [docs/exec-engines.md](docs/exec-engines.md)
- [docs/previews.md](docs/previews.md)

## Security model

Security-sensitive guarantees include:

- forced Postgres row-level security for tenant-scoped data;
- tenant context established from authenticated requests;
- concealed plaintext excluded from unauthorised contexts;
- provider credentials encrypted at rest and referenced indirectly;
- permission checks enforced server-side;
- append-only audit and provenance records;
- validated declarative user content instead of user-authored executable code;
- quarantining and inspection of imported bundles.

Security issues should not be filed publicly. Follow
[SECURITY.md](SECURITY.md) to use GitHub's private vulnerability-reporting channel.

## Workflow packs

A workflow pack can define:

- process definitions and phase visibility;
- entity schemas and state machines;
- rule systems and deterministic tools;
- behaviour axes;
- vocabulary overlays;
- sample smoke data.

Packs are content, not application plugins. They are validated before use and do not
execute arbitrary user code.

The built-in default workflow provides a general structured discussion so a fresh
installation works before any external pack repository is configured.

## Portability

Pyrrhula exports native `.pyr` bundles containing versioned, inspectable content and
provenance. Import treats every bundle as attacker-controlled input: content is scanned,
validated, and quarantined where necessary.

Character Card V3 import and export are supported for interoperability. See
[docs/portability.md](docs/portability.md).

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) and
[docs/agent-guide.md](docs/agent-guide.md) before changing the core. The project enforces
strict boundaries around vocabulary, tenancy, secrets, executable content, and replay.

By opening a pull request, you agree to the
[Contributor License Agreement](CLA.md).

## Licence

The engine is licensed under the GNU Affero General Public License. See
[LICENSE](LICENSE). Workflow packs and samples may use their own licences.

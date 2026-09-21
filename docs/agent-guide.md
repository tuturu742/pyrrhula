# Coding agent guide

This guide is the conventions and definitions reference for implementing changes in
Pyrrhula. Read it together with the hard rules in `CLAUDE.md`; use the code, tests, and
current user-facing documentation to understand the behaviour of a specific subsystem.

## 1. How to use this guide

Before editing:

1. Read `CLAUDE.md`.
2. Identify the subsystem and inspect its models, service, adapters, routes, and tests.
3. Find the architecture, isolation, leak, replay, or pack test that guards the relevant
   invariant.
4. Make the smallest coherent change.
5. Run the narrow tests first, then the broader checks appropriate to the change.

Do not infer a convention from one accidental implementation detail. Prefer repeated
patterns, explicit tests, and the definitions below.

## 2. Package boundaries

### Core

`packages/core/` contains domain-neutral business models and services. It may define and
consume ports, but it must not depend on adapters, API routes, worker jobs, deployment
code, or the web application.

Core names describe general concepts:

- tenant
- workspace
- session
- phase
- agent
- knowledge
- entity
- secret
- resolution
- report
- workflow

Names tied to a particular vertical belong in workflow packs and vocabulary overlays.

### Adapters

`packages/adapters/` implements core ports. Adapter packages may depend on core ports and
models. They contain provider-specific behaviour for storage, queues, identity, model
access, execution environments, previews, permissions, and similar integrations.

An adapter must not redefine core policy. For example, a storage adapter may enforce a
port contract, but visibility and disclosure policy remain core concerns.

### API

`packages/api/` owns HTTP transport, request authentication, middleware, dependency
composition, route schemas, streaming endpoints, and the MCP server surface.

Routes should be thin:

1. resolve authenticated tenant and actor context;
2. parse and validate transport input;
3. call a core service;
4. map the result to the transport response.

Do not implement business rules directly in route handlers.

### Worker

`packages/worker/` owns asynchronous job entry points and composition for long-running or
retryable work. Worker jobs call core services through the same boundaries as the API.

A worker job must establish tenant scope explicitly and preserve idempotency across
retries.

### Evaluation

`packages/eval/` contains evaluation-only scenarios, runners, arms, metrics, and reports.
Production code must not import evaluation packages.

### Web

`web/` is the React client. It may improve usability and hide unavailable controls, but
server-side services remain responsible for authentication, authorisation, validation,
and isolation.

### Workflow packs

Built-in and external workflow packs contain declarative content. They may define
vocabulary, schemas, finite-state machines, process definitions, rule systems, behaviour
axes, and deterministic tool declarations. They do not execute arbitrary code and do not
add imports to the application.

## 3. Invariants

### INV-1: Dependencies point inward

Core is independent of delivery and infrastructure concerns. API and worker packages
compose core services with adapters. Architecture tests enforce important import edges.

When adding an import, ask whether the depended-on package is conceptually more central
than the importing package. If not, introduce or extend a port.

### INV-2: Tenant isolation is enforced by the database

Every tenant-scoped table has a `tenant_id`, row-level security enabled and forced, and
policies that filter through the active tenant context.

Application filtering is useful but is not the security boundary. Queries must execute
inside `tenant_scope()` using the application database role.

Every new tenant-scoped table needs a negative isolation test proving another tenant
cannot read or mutate its rows.

### INV-3: Audit and event history is append-only

Audit, event, provenance, decision, and equivalent history records are immutable.
Corrections append another record rather than rewriting history.

Hash-chain and canonical-serialisation behaviour must remain deterministic.

### INV-4: Context assembly is explicit and inspectable

A generation context is assembled from resolved visibility, selected knowledge, entity
fields, history, directives, and authorised secret material.

The associated context manifest records what was selected. Replay uses recorded versions
and inputs rather than mutable current state.

### INV-5: Concealment is exclusion

Concealed plaintext is absent from generation context. Prompt instructions are not a
substitute for exclusion.

The disclosure gate receives only information required to classify disclosure, including
gists where configured. It must not receive concealed plaintext and must fail closed on
errors or invalid output.

Leak checks are defence in depth. They do not justify passing plaintext to an
unauthorised model.

### INV-6: User-authored behaviour is declarative

Supported authoring surfaces are validated data such as JSON Schema, finite-state
machines, CEL, process definitions, rule systems, and vocabulary overlays.

Do not introduce `eval`, arbitrary Python, arbitrary shell commands, or in-process
execution of uploaded code.

### INV-7: Effects are idempotent

Operations that can be retried use stable idempotency keys and do not duplicate external
effects. Retries must not create duplicate audit records, notifications, jobs, or
mutations unless duplication is explicitly part of the contract.

### INV-8: Deterministic results retain their inputs

Rule-system and deterministic-tool records include the expression, actor data or resolved
modifiers, seed where relevant, tool version, and result needed for verification.

Do not recompute old results from mutable entities.

### INV-9: Packs remain independent content

The core does not special-case a shipped pack, and packs do not import or execute core
implementation code. Generic capabilities belong in core; domain meaning belongs in
packs.

## 4. Tenancy conventions

### Tenant context

Tenant identity comes from authenticated request or job context, not from a body field
trusted in isolation. Establish tenant scope before using a tenant-scoped repository.

Do not retain a scoped database session beyond the scope's lifetime. Background work must
open its own scope.

### IDs are not authorisation

UUIDs and other opaque identifiers prevent accidental collision; they do not grant
access. A lookup by ID must still be tenant-scoped and permission-checked.

### Shared and isolated deployment modes

Deployment shape may change how tenant databases are routed, but services should use the
tenant-router and scope abstractions rather than branching on deployment mode.

### Migrations

For a new tenant-scoped table:

1. add `tenant_id`;
2. add appropriate keys and indexes;
3. enable and force RLS;
4. add policies for the application role;
5. update the baseline where repository conventions require it;
6. add a cross-tenant negative test.

Do not weaken migration security to make local setup easier.

## 5. Knowledge and retrieval

### Knowledge source and entry

A source is an authored or ingested body of material. Entries are versioned units used by
activation and retrieval. Published chunks are derived retrieval material and preserve
their source/version relationship.

### Classes and scopes

Knowledge classes describe the kind of material. Scopes describe who may receive it.
Both are resolved before ranking. Retrieval must not fetch broadly and discard
unauthorised results after assembly when the visibility predicate can be pushed into the
query.

### Activation

Activation chooses the eligible knowledge set for a turn using process visibility,
constant knowledge, keyword rules, and other supported selectors.

### Retrieval

Hybrid retrieval combines sparse and dense candidates, applies visibility and version
constraints, fuses rankings, optionally reranks, and fills a class-aware token budget.

A score is not permission. No ranker or cache may reintroduce an item excluded by
visibility.

### Caching

Cache keys include all inputs that affect result eligibility and ordering, including
tenant, scope, versions, query identity, and relevant model/configuration versions.
Cached results are validated against current authorisation constraints where required.

## 6. Context assembly and citations

Context assembly is a pipeline, not prompt concatenation scattered across services.

Typical inputs include:

- process and phase instructions;
- persona and behaviour directives;
- authorised knowledge excerpts;
- authorised entity fields;
- visible session history;
- deterministic tool results;
- authorised secret disclosures.

Layout is responsible for ordering and budget presentation, not for bypassing visibility.

Citations refer to material actually present in the context. Citation validation must
reject references to absent, stale, or inaccessible material.

A context manifest should be sufficient to explain why an item appeared and to identify
the exact version used.

## 7. Secrets

### Holder

A holder relationship says which persona owns or may know a secret. It does not imply
that every mode includes the plaintext in every turn.

### Directive

A directive tells a holder how to behave around a secret without necessarily revealing
the secret text to the generation model.

### Gist

A gist is a limited topical representation used where the disclosure gate needs enough
information to classify a turn. It is not a place to copy or lightly paraphrase
plaintext.

### Modes

- `excluded`: plaintext does not enter generation context;
- `trust`: a holder's own authorised plaintext may enter its context;
- `gate`: the gate decides conceal, hint, or reveal and fails closed.

A persona never receives another persona's secret merely because both are in the same
session.

### Exports and reports

Export mode and report visibility are separate decisions from generation visibility.
Tests must prove that concealed plaintext is absent from unauthorised bundles, reports,
manifests, logs, and delegated work briefs.

## 8. Process execution

### Process definition

A process definition is versioned declarative data describing phases, actors, visibility,
budgets, awaits, and transitions.

### Session

A session pins the process and relevant content versions used for execution. It has a
lifecycle and may be active, awaiting input, completed, archived, or faulted according to
the supported model.

### Phase and turn scheduling

The interpreter determines available work from process state. The scheduler chooses
eligible actors under the phase's actor rules and pacing constraints.

Reactive behaviour such as chattiness may influence scheduling only through validated
capabilities. Prompt text alone must not claim to enforce scheduling.

### Awaits and timeouts

An await records what input is required and what timeout behaviour applies. Resuming must
be idempotent and must restart execution where needed without duplicating completed work.

### Checkpoints and replay

Checkpoints record enough state to resume safely. Replay consumes recorded manifests,
versions, events, and deterministic results. It must not silently substitute current
content for pinned content.

## 9. Entities and rule systems

### Entity schema

Entity schemas define fields, constraints, semantic tags, views, and state machines.
Values are validated on creation and mutation.

### Semantic tags

Tags express generic presentation or behaviour meaning such as identity, resource,
status, relationship, or progression. The core and web renderers use tags rather than
domain-specific field names.

### Mutation

Mutations validate field types, schema constraints, state-machine rules, tenant scope,
permissions, and concurrency expectations before committing.

### Rule system

A rule system validates expressions, resolves modifiers from authorised actor data,
executes a deterministic implementation, and records a resolution result.

Models may request deterministic tools, but they do not invent authoritative outcomes.
The validated tool result is authoritative.

## 10. Agents and models

### Persona and agent

A persona is authored identity and behaviour configuration. Runtime agent records bind
that configuration to a session and retain the version used.

A model connection is infrastructure configuration. Per-persona settings may override
connection defaults through the documented settings-resolution chain.

### Generation

Generation calls pass through the model-provider port. Provider-specific parameter
repair belongs in the adapter. Core services should not branch on provider brand.

Record usage and provenance without logging credentials or concealed context.

### Behaviour axes

Axes are validated capabilities with declared bindings. A prompt-directive binding
changes instructions; a sampling binding changes supported generation parameters; a gate
binding invokes an enforced control. Do not imply that one binding provides the guarantee
of another.

## 11. Permissions and identity

Authentication establishes who the actor is. Tenant context establishes where the action
occurs. Permission checks establish whether the actor may perform it.

Use `PermissionService` rather than duplicating role comparisons. Platform-admin
operations are distinct from tenant roles and must remain explicitly gated.

Hosted-git credentials and provider credentials are secrets. Store encrypted values,
return references or redacted metadata, and resolve plaintext only at the narrow point of
use.

## 12. Jobs, queues, and effects

Background jobs carry stable identifiers, tenant context, and serialisable inputs.
Workers reopen repositories and scopes rather than receiving live sessions or clients.

Retry policy must distinguish transient provider/infrastructure errors from invalid input
and invariant failures.

Notifications, delegated tasks, exports, previews, and other effects use idempotency
controls so queue retries do not duplicate work.

## 13. API conventions

- Keep `/api` prefix handling consistent across direct and proxied deployments.
- Validate request and response models.
- Resolve tenant and actor context through dependencies.
- Enforce permissions before returning scoped records.
- Avoid leaking existence across tenants through distinguishable errors.
- Stream only data the caller could retrieve through the non-streaming service boundary.
- Do not expose encrypted credential payloads.
- Keep route inventory tests current when adding or removing endpoints.

MCP handlers follow the same service and permission boundaries as HTTP routes. MCP is a
transport, not a privileged path around core policy.

## 14. Web conventions

Use the generated or shared API-client types where available. Keep server state in the
established query layer and local presentation state in components or the relevant
store.

The UI should:

- display loading, empty, error, and permission-denied states;
- avoid domain-specific components in generic entity rendering;
- use vocabulary labels instead of hard-coded vertical terms;
- preserve accessibility for dialogs, forms, controls, and native colour-scheme
  behaviour;
- treat server responses as authoritative.

Never rely on a disabled or hidden button as the only permission check.

## 15. Testing conventions

### Unit and service tests

Place focused tests near the package they exercise. Prefer deterministic fakes that
implement the real port contract.

### Architecture tests

Use `tests/architecture/` for import boundaries, pack independence, route shape, and
other repository-wide structural rules.

### Isolation tests

Use `tests/isolation/` for tenant and workspace separation. Negative tests should create
data under one tenant, switch scope, and prove reads and writes cannot cross the boundary.

### Leak tests

Use `tests/leak/` to prove concealed plaintext does not reach contexts, gates, exports,
reports, delegated briefs, or other protected seams.

Tests should assert absence from the complete payload, not only from one expected field.

### Replay tests

Use `tests/replay/` to prove recorded inputs reproduce context and deterministic outcomes
without consulting mutable current state.

### Pack tests

Use `tests/packs/` for declarative pack contracts and cross-pack independence.

### Integration dependencies

Some tests skip when Postgres or Redis is unavailable. Check the test summary. A skip is
not a pass for changes touching isolation, queues, streaming, or persistence.

## 16. Error handling

Invalid authored content should produce actionable validation errors. Security-sensitive
failures should fail closed and avoid echoing protected values.

Do not catch broad exceptions merely to continue with weaker behaviour. If fallback is
part of the contract, make it explicit, observable, and tested.

Logs should include stable identifiers and enough context for operation without including
credentials, access tokens, secret plaintext, or full prompts.

## 17. Documentation and configuration

Document operator-facing environment variables in `docs/configuration.md`. The
documentation checker expects variables read by code and variables listed in the table to
remain synchronized.

Use dedicated documentation for model settings, MCP, execution engines, portability,
previews, installation, and self-hosting. Keep examples aligned with supported deployment
shapes.

Do not document a feature as enforced when it is only advisory.

## 18. Completion checklist

Before declaring a change complete, ask:

- Did the change preserve package boundaries?
- Is every data access correctly tenant-scoped?
- Could concealed plaintext reach a new payload, log, cache, report, or export?
- Did any user-authored data become executable?
- Are permission checks server-side and centralised?
- Are retries idempotent?
- Are versions and deterministic inputs recorded?
- Does the change introduce domain vocabulary into generic code?
- Do migrations preserve RLS and forward-only history?
- Did the relevant negative tests run rather than skip?
- Are configuration and user-facing documentation current?
- Is the diff limited to the requested work?

If a required behaviour cannot be implemented without violating an invariant, surface the
conflict instead of adding an exception.

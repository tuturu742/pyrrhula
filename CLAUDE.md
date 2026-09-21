# CLAUDE.md

Instructions for coding agents working in this repository.

## Source-of-truth order

1. `docs/agent-guide.md` — conventions and definitions.
2. The hard rules in this file — **authoritative.** Do not weaken, bypass, or "improve" on them.

## Hard rules

### Preserve the architecture boundary

`packages/core/` is domain-neutral. It must not contain vocabulary tied to a particular
workflow or vertical. Domain concepts belong in workflow packs and vocabulary overlays.

Dependencies point inward:

- core defines domain-neutral models, services, and ports;
- adapters implement ports;
- API and worker packages compose services and adapters;
- workflow packs provide domain content, not executable application code.

Do not import API, worker, adapter, or web concerns into core.

### Tenant isolation is structural

Every tenant-scoped database row carries `tenant_id`. Tenant-scoped database access must
run through the repository's tenant scope, with row-level security enabled and forced.

Never:

- trust a request-supplied tenant identifier without resolving it from authenticated
  context;
- add an unscoped query as a convenience;
- disable RLS to make a test pass;
- rely only on application filtering where the database can enforce isolation.

A new tenant-scoped table requires an isolation negative test in the same change.

### Concealed information is excluded

A secret that is not available to a participant must be absent from that participant's
generation context. Instructions such as “do not reveal this” are not a security control.

The disclosure gate sees gists and metadata, not concealed plaintext. It fails closed.
Post-generation leak checks are defence in depth and do not replace context exclusion.

Do not log, report, export, cite, embed, cache, or place concealed plaintext in a context
where it is not authorised.

### No user-authored code execution

User-authored behaviour is declarative: JSON Schema, finite-state machines, CEL, and the
supported process DSL. Do not add `eval`, dynamic Python, shell interpolation, uploaded
plugins that execute in-process, or a “temporary” code escape hatch.

If the declarative surface cannot express a required operation, add a narrow,
domain-neutral primitive with validation and tests.

### Ports remain real seams

Core code depends on ports, not concrete providers. Provider-specific configuration and
behaviour belong in adapters and composition roots.

Do not instantiate infrastructure clients in core services. Do not make tests depend on
live third-party services when a contract test or deterministic fake can prove the same
behaviour.

### Audit records are append-only

Do not update or delete append-only audit, event, decision, or provenance records.
Corrections are new records. Preserve canonical serialisation and hash-chain behaviour.

Effectful operations must retain their idempotency guarantees.

### Determinism must be recorded

Anything presented as replayable must carry the inputs needed to reproduce it: versions,
seeds, manifests, selected records, and deterministic tool results. Do not silently read
mutable “latest” state during replay.

### Permissions go through the permission service

Do not scatter role checks through routes or UI code. Authorisation belongs behind the
permission service and must be enforced server-side. Hiding a control in the web UI is not
an authorisation check.

### Keep packs as content

Workflow packs may contain schemas, process definitions, rule-system definitions,
vocabulary, and other validated declarative assets. They must not import application code
or become a second plugin runtime.

### Migrations move forward

Do not edit an applied migration to change existing history. Add a new migration. Keep
the SQL baseline and migration path consistent with the repository's migration
conventions.

## Working style

Read the relevant implementation, nearby tests, and `docs/agent-guide.md` before changing
code. Prefer the smallest change that satisfies the request.

Do not:

- broaden scope while “cleaning up”;
- rename unrelated symbols;
- reformat unrelated files;
- weaken an invariant or test;
- turn a required integration test into a mock-only test;
- silently reinterpret acceptance criteria.

When a request conflicts with a hard rule, stop and explain the conflict rather than
working around it.

## Verification

Run the narrowest relevant tests while developing, then the repository checks appropriate
to the change:

```bash
uv sync --extra dev
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Some isolation and integration tests require Postgres, Redis, and fetched workflow packs.
A skipped test is not evidence that the guarded behaviour works. Check test output and
provide the required services when the change touches those guarantees.

For web changes, use the package manager and scripts pinned in `web/package.json` and
`web/pnpm-lock.yaml`.

## Changes and commits

Keep one logical change per commit. Commit messages should explain why the change is
correct, not merely restate the diff.

Never commit credentials, generated local state, dependency caches, build output, or
environment files containing secrets.

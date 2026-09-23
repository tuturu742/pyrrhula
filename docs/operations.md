# Operating a deployment

Everything an operator does that is not a request: deleting a tenant for real, resetting
a password nobody can email, re-sealing credentials after a key change, seeding and
verifying a rebuilt stack. These are command-line tasks run inside a running container,
plus a small admin console for the things that are safe over HTTP.

If you are looking for the daily rebuild loop, that is
[`docs/runbook-daily.md`](runbook-daily.md). If you are looking for what content a
deployment loads and from where, that is
[`docs/packs-and-samples.md`](packs-and-samples.md).

> **Two different things are called "purge."** The installer's `--purge` destroys the
> *deployment* — namespaces, volumes, database and all — and is a rebuild step. The
> `core.tenancy.purge` CLI on this page deletes *one tenant's rows* from a database that
> keeps running. They share a word and nothing else.

## Running a command inside the deployment

Every command below runs in the api container, which has the database credentials and
the application code. Pick the line for your deployment and substitute the command:

```bash
# Kubernetes
kubectl -n pyrrhula exec deploy/pyrrhula-api -- <command>

# Compose
podman exec pyrrhula_api_1 <command>          # or docker exec
```

The worker container works for any of these too; the api one is simply always there.

### What exec access means

**Being able to `exec` into these containers is equivalent to being a database
superuser.** Not because of the tools on this page — because the container's environment
holds `PYRRHULA_DATABASE_URL`, the owner DSN, and anyone with a shell can read it and
connect directly:

```
$ kubectl -n pyrrhula exec deploy/pyrrhula-api -- python -c "..."
connected as pyrrhula | superuser: True
```

So the CLI tools below add convenience, never capability: an attacker with exec does not
need `core.tenancy.purge` to delete a tenant, and cannot be stopped from doing so by
removing it. **The control is authorization on exec itself** — Kubernetes RBAC on
`pods/exec`, and host access for compose — not anything in this repository.

The api process genuinely needs that DSN: defining an entity schema issues
`ALTER TABLE`/`CREATE INDEX` through `admin_ddl_session`, and plugin-repository sync
writes NULL-tenant rows through `admin_registry_session`, neither of which the
RLS-scoped app role may do. (`docker/compose.selfhost.yml` still describes this DSN as
"used ONLY by the migrate entrypoint"; that comment predates both features and is wrong
— the api, worker and admin containers all receive it.)

Two consequences worth stating plainly:

- **Nothing on this page is audited.** A tenant purge writes no `audit_log` row, because
  the rows it would write to are the rows it is deleting. The evidence that it happened
  is your shell history and the absence of the tenant. An operation you need attributable
  should go through the admin console or the API, which do write audit rows.
- **Least privilege for these roles is unfinished.** The honest fix is a narrower
  registry/DDL role with grants only on the tables those two features touch, so the api
  container stops holding superuser. That is a schema-and-migration change nobody has
  made yet.

## Deleting a tenant, for real

`core.tenancy.purge` is the only sanctioned hard delete in the system. Everything
reachable from a request can *archive* — soft-delete — because the app role is RLS-scoped
to one tenant and REVOKE'd from deleting the append-only tables (`audit_log`,
`session_event`, `resolution_record`, …). Reclaiming those rows needs the admin role,
which both bypasses RLS and holds the DELETE grant.

The admin console deliberately has no button for this: see
[Why not in the UI](#why-hard-deletion-is-not-in-the-admin-console).

**It is a dry run unless you pass `--yes`.**

```bash
# What would go, and nothing else happens
kubectl -n pyrrhula exec deploy/pyrrhula-api -- \
  python -m core.tenancy.purge --tenant acme

# Do it
kubectl -n pyrrhula exec deploy/pyrrhula-api -- \
  python -m core.tenancy.purge --tenant acme --yes
```

| Flag | Effect |
|---|---|
| `--tenant <slug>` | one tenant, named exactly |
| `--slug-prefix <prefix>` | every tenant whose slug starts with this — the bulk path for test leftovers (`isolation-`, `sched-`, `pd-`, …) |
| `--archived-only` | keep the tenant, delete only its archived personas, sessions, knowledge sources, process definitions and unreferenced connections. Requires `--tenant` |
| `--except <slug>` | keep this one (repeatable), e.g. `--slug-prefix '' --except dev` |
| `--yes` | actually delete; without it the command changes nothing |

The reserved library tenant is always skipped, with or without `--yes`.

**It is slow on large selections and that is not a hang.** The tool reports per-tenant
row counts before deleting, and deletes one tenant per statement, so a prefix matching
twenty thousand tenants issues well over a hundred thousand queries. A single tenant is
quick; a bulk prefix is a "start it and go and do something else" operation.

**Order matters inside a tenant, and the tool knows it.** Archived personas go first via
their principals — which cascades persona → session → message/manifest — because that is
what frees connection agents for the `RESTRICT`-referenced delete that follows. Do not
reproduce this by hand in psql.

### Why hard deletion is not in the admin console

`admin_purge_session` is a superuser connection with no tenant scoping at all, and its
own docstring forbids importing it from request-path code. Putting it behind an HTTP
endpoint would mean a request that can delete any tenant's entire history, including the
append-only tables the whole design revokes DELETE on. The console offers **deactivate**
instead, which blocks login and every request for that tenant while leaving the data
intact and the action audited — which is what "remove this customer" almost always
actually means. Reach for the CLI only when the rows themselves must go.

## Resetting a password

There is no email delivery, so a locked-out user is an operator task. Prints a freshly
generated password once; the user should change it after logging in. Refuses an unknown
tenant or email rather than creating anything.

```bash
podman exec pyrrhula_api_1 python -m worker.reset_password acme someone@example.com
```

## Re-sealing credentials

Provider API keys, repository tokens and registry passwords are sealed with AES-256-GCM
and startup refuses to run unencrypted. If a deployment carries rows from before
encryption existed, this seals them with the configured key.

```bash
podman exec pyrrhula_api_1 python -m worker.reencrypt          # dry run
podman exec pyrrhula_api_1 python -m worker.reencrypt --yes    # for real
```

Idempotent: rows already prefixed `enc1:` are skipped, so re-running finishes a partial
run. It needs `PYRRHULA_ENCRYPTION_KEY` on the container. Rotating to a *new* key is a
different operation — decrypt with the old, encrypt with the new — and no tool does it.

## The timeout sweep

`worker.timeouts` fires the `on_timeout` transition for any await whose deadline has
passed, and sends due reminders on the same tick. It is a small periodic loop rather than
a job-queue handler, because it needs cross-tenant reach without widening the no-RLS
exception list. Both deployments run it; you would only start one by hand when
diagnosing why a timed-out session never moved.

```bash
podman exec pyrrhula_worker_1 python -m worker.timeouts
```

## Populating and checking a deployment

| Command | What it does |
|---|---|
| `python scripts/fetch_plugins.py` | clone the pinned workflow packs into `.plugins/`. Run before an image build; `PYRRHULA_PLUGINS_STRICT=1` makes a stale pack a failure rather than a warning |
| `python scripts/seed_samples.py --secrets-dir … --samples-dir … --samples …` | create the model connections, import each sample's `.pyr`, bind personas by role, register its repositories and MCP servers, load its pack |
| `python scripts/start_sample_session.py --tenant … --flow … ` | start a session in a seeded tenant, resolved by slug, flow key and persona name |
| `python scripts/build_samples.py <out_dir>` | rebuild the shippable `.pyr` bundles from their specs |
| `python scripts/check_env_docs.py` | fail if any environment variable the code reads has no row in `docs/configuration.md` |
| `python scripts/verify_deploy.py exec [rpg swe]` | drive the standing post-redeploy scenarios against the live stack and assert concrete outcomes; exits non-zero on the first failure, so a rebuild loop can gate on it |

`verify_deploy.py` is stdlib-only and runs from the host, reaching the stack through
podman by default. Point it at Kubernetes with environment variables rather than editing
it:

```bash
export PYRRHULA_VERIFY_API="http://pyrrhula.localhost/api"
export PYRRHULA_VERIFY_PG_EXEC="kubectl -n pyrrhula exec -i statefulset/postgres --"
export PYRRHULA_VERIFY_API_EXEC="kubectl -n pyrrhula exec -i deploy/pyrrhula-api --"
python scripts/verify_deploy.py exec
```

## The admin console

A separate app from the tenant-facing one, for the platform operator rather than any
tenant. It is authenticated by JWT as a platform admin; `PYRRHULA_ADMIN_TOKEN` is a
header-based fallback for the standalone deployment shape.

| Page | What it answers |
|---|---|
| **Tenants** | who exists on this deployment, how many members and agents each has; create one, deactivate or reactivate it, pin its workflow and vocabulary overlay, manage its users, grant MCP servers, set its egress policy, verify its audit chain |
| **Plugin repositories** | which workflow-content repositories this deployment trusts and at which commit; register one at a pinned ref, upload an archive for an air-gapped install, re-sync, remove |
| **Retrieval models** | which embedding and rerank models are installed; download one, upload one, choose which is active. A freshly purged deployment has none, and **every session fails at context assembly until one finishes** |
| **Assistant model** | the deployment-level default model for the workspace assistant |

What it deliberately cannot do: delete a tenant (see above), read any tenant's session
content, or change a tenant's own data. Deactivation is the reversible, audited stand-in
for removal.

## Troubleshooting

**A session sits in one phase and never advances.** Look for a failed `advance_session`
job and a `completed_operation` row still `in_progress` for that turn — a worker that
died mid-turn leaves a claim behind, and nothing retries a job that already failed. The
lease reclaims an abandoned `in_progress` row; a `failed` row is sticky on purpose,
because re-running an operation that failed *after* its side effects would repeat them.

```sql
select kind, status, attempts, error from job
 where payload->>'session_id' = '<id>' order by created_at desc limit 5;
select status, idempotency_key from completed_operation
 where idempotency_key like 'turn:<id>%' order by created_at desc limit 5;
```

**Every turn fails at context assembly.** No embedding model. A purge takes the model
cache with it and the installer does not download one, because which model a deployment
wants is the operator's choice.

```sql
select status, result->>'outcome' from job
 where kind='download_retrieval_models' order by created_at desc limit 1;
```

`done` with outcome `downloaded` is the only green — a job can be `done` with outcome
`failed`, because the outcome lives in the result payload for the console to render.

**A pack fix does not reach sessions.** Three separate things, each of which looks like
the others: the commit is not in `deploy/plugins.json`; the pin is right but the fetch
fell back to a cached copy (it warns, naming both refs); or the pack loaded but the
session started on a superseded flow version. A session resolves its phases against the
definition row it started on, so a fix reaches *new* sessions only.

**A second session in a workspace behaves as if the first never ended.** It half did.
Entities and persona bindings are workspace-scoped and outlive the session that made them;
only the transcript is session-scoped. A second campaign therefore opens with the first
one's characters in context, and `entity_create` with `bind_to_self` refuses because the
persona is still bound. Archiving the first session hides it without removing what it
made. A clean re-run is a purge and a reseed of the tenant, not an archive — see "What a
session carries" in [`docs/agent-guide.md`](agent-guide.md).

**An imported tenant reads as empty.** Check the source's `current_version_id` — entries
can import and publish while the source points at no version, which makes the authoring
UI show nothing. Also check secrets: an import with no authoring principal skips every
secret and otherwise succeeds, so a mystery imports looking complete with none of its
private briefs.

**A preview will not start, or serves a directory listing.** A listing means the recipe
never applied: the repo's `pyrrhula-preview.json` is read at the ref being previewed, so
it must be on that branch. A refusal names the field. See
[`docs/previews.md`](previews.md).

**A delegation opened a pull request containing a placeholder.** Look for
`codegen.fallback_to_scaffold` and `codegen.no_api_key` in the worker log. The pull
request body ends with `[scaffold]` rather than `[model]` when this happened — that
marker is the fastest way to tell a real change from a stub.

`fallback_to_scaffold` **without** a preceding `codegen.no_api_key` means the credential
resolved and the call still failed: read the `error=` on the fallback line rather than
assuming a missing key. A provider authentication error there points at a request built
somewhere other than the first one — the format-repair retry, for instance, which fires
only when a model's first answer carries no `===FILE:` blocks and so makes the whole
thing look intermittent.

**Something is slow and it might be data volume.** The tenant listing costs two queries
per tenant, so a database carrying tens of thousands of them — a development box that has
run the test suite for months, typically — makes any page touching it take minutes. Count
first, then purge by prefix:

```sql
select count(*) from tenant;
select split_part(slug,'-',1) as prefix, count(*) from tenant group by 1 order by 2 desc limit 10;
```

## See also

- [`docs/runbook-daily.md`](runbook-daily.md) — purging and rebuilding whole deployments
- [`docs/packs-and-samples.md`](packs-and-samples.md) — where a deployment's content comes from
- [`docs/configuration.md`](configuration.md) — every environment variable
- [`docs/install.md`](install.md) — standing a deployment up in the first place

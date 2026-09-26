# Operating a deployment

Everything an operator does that is not a request: deleting a tenant for real, resetting
a password nobody can email, re-sealing credentials after a key change, checking a
rebuilt stack. These are command-line tasks run inside a running container,
plus a small admin console for the things that are safe over HTTP.

If you are looking for what content a deployment loads and from where, that is
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
RLS-scoped app role may do. The migrate, api and worker containers all receive it.

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

## Who may join an organization

Two different questions, and they were not equally guarded:

* **Creating a new organization** — `POST /auth/signup`, gated by the **Self-serve
  signup** switch in the admin console (`PYRRHULA_ALLOW_TENANT_SIGNUP` is only what a
  fresh deployment starts with). It cannot be used to reach an existing organization: a
  slug collision allocates a new suffix rather than joining the one that is there.
* **Joining an existing organization** — `POST /auth/register`, which names its
  organization in the `X-Pyrrhula-Tenant` header. This one checked nothing but the per-IP
  rate limiter, so anyone who could reach the API could obtain a membership, and a session
  token, inside any organization on the deployment.

Each organization states a policy, in the admin console under **Tenants → Joining** on
its row (where pending applications are approved or rejected):

| Policy | What happens to a stranger who tries |
| --- | --- |
| `closed` | 403. You create every account yourself. **The default.** |
| `request` | Their application is queued for you to approve or reject. No account, no membership, no token exists in the meantime, and they cannot sign in. |
| `open` | They get a `viewer` account immediately — the old behaviour, now chosen rather than assumed. |

`closed` is the default because the behaviour it replaced was the vulnerability: a
deployment that upgraded into a permissive default would have gained the setting and kept
the hole. An organization that has not chosen is `closed`; there is no deployment-wide
default to loosen that. A stored value that is not one of the three — a typo, a hand-edited row
— is read as `closed`, because failing the other way puts an organization on the internet
over a misspelling.

Under `request`, the application holds an argon2 hash of the password the applicant
chose, so approving mints the account without asking them to choose again. Approving is
where you pick their role. Rejecting discards the hash.

```bash
# from a script, as a platform admin ($TOKEN from POST /auth/login with
# X-Pyrrhula-Tenant: admin)
curl -X PUT "$API/admin/tenants/$TENANT/registration-policy" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"policy": "request"}'

curl "$API/admin/tenants/$TENANT/registration-requests" \
  -H "Authorization: Bearer $TOKEN"
```

Seeded sample tenants have no human accounts at all — seeding creates personas, which are
principals, not people. Add one under **Users** on the tenant card, or with
`POST /admin/tenants/{id}/users`.

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

## How many workers

One worker advances one session at a time. A deployment running several sessions at once
serialises them, and the effect is easy to misread: a session sits at `seq=0` for twenty
minutes looking stuck while another holds the worker through a long model turn.

Kubernetes scales the ordinary way, and the session claim plus its heartbeat is what makes
more than one safe — a second worker that finds a session claimed declines and says so
(`advance.already_claimed`) instead of colliding:

```bash
kubectl -n pyrrhula scale deploy/pyrrhula-worker --replicas=3
```

### A long job is not a dead one

The job queue has its own claim, separate from the session claim above, and it works the
same way. A worker that dies mid-job leaves its row in `claimed` with nobody running it,
so the row carries a lease: once it has been quiet for `lease_seconds` (900 by default)
another worker may take it, and after three attempts it is failed and left alone rather
than crash-looping.

The worker calls `heartbeat` every 30 seconds while its handler runs, so the lease
measures **silence, not duration**. This matters for the delegation handlers in
particular: a codegen rework that builds a container, runs a suite and repairs its output
regularly runs past fifteen minutes. Before the heartbeat existed, the queue read that as
a dead worker and handed the job to a second one — the tenant paid for the same generation
twice and two workers pushed to the same place, while the job's own log looked normal.

Each beat carries the attempt number the worker claimed at. A worker that really did go
quiet long enough to be reclaimed therefore cannot come back and hold the new holder's
lease open: its beat returns false, it logs `worker.claim_lost`, and it stops.

If you see one job id claimed twice in the worker logs, check that the workers are on an
image that has the heartbeat before looking for anything subtler:

```bash
kubectl -n pyrrhula logs -l app=pyrrhula-worker --tail=200 | grep -E "job_claimed|claim_lost" | sort
```

Restarting workers during long jobs is safe but not free: the killed worker stops beating,
and its job is only picked back up once the lease elapses, so expect up to
`lease_seconds` of apparent idleness after a rollout.

Compose fixes `container_name` on the worker, so `podman compose` cannot scale it. Running
extra workers by hand works, and **two details bite**:

* `--dns` is not inherited. The compose files set a resolver on api and worker because a
  host whose only nameserver is `systemd-resolved` at `127.0.0.53` gives a container a
  loopback address that means nothing in its namespace. Without it the hand-started worker
  claims a job and fails it with `Cannot connect to host api.deepseek.com:443`.
* **Do not build the environment with `--env-file`.** `PYRRHULA_EXEC_ENGINES` is
  multi-line JSON and the env-file format cannot carry a newline: it keeps the first line
  and discards the rest, and the worker then fails every job that touches an execution
  engine with `PYRRHULA_EXEC_ENGINES is not valid JSON`. Pass each variable as its own
  `--env` argument, which survives newlines, and check one afterwards:

```bash
podman exec pyrrhula_worker_2 python -c \
  "import json,os; json.loads(os.environ['PYRRHULA_EXEC_ENGINES']); print('ok')"
```

## Populating and checking a deployment

| Command | What it does |
|---|---|
| `python scripts/fetch_plugins.py` | clone the pinned workflow packs into `.plugins/`. Run before an image build; `PYRRHULA_PLUGINS_STRICT=1` makes a stale pack a failure rather than a warning |
| `python scripts/check_env_docs.py` | fail if any environment variable the code reads has no row in `docs/configuration.md` |

Setting a sample up is a sequence of steps in the product, described by each sample's
README; there is no seeding script.

## The admin console

The platform operator's pages in the same UI, reached by signing in with organization
`admin` (or, on a single-organization deployment, as that organization's owner). There
is no separate app and no shared token: every admin call carries a platform admin's own
login.

| Page | What it answers |
|---|---|
| **Tenants** | who exists on this deployment, how many members and agents each has; create one, deactivate or reactivate it, pin its workflow (which applies that workflow's vocabulary overlay), manage its users, decide who may join and approve applications, grant MCP servers, set its egress policy, verify its audit chain; and the deployment-wide **Self-serve signup** switch |
| **Plugin repositories** | which workflow-content repositories this deployment trusts and at which commit; register one at a pinned ref, upload an archive for an air-gapped install, re-sync, remove |
| **Models** | which embedding and rerank models are installed; download one, upload one, choose which is active. A freshly purged deployment has none, and **every session fails at context assembly until one finishes** |
| **Assistant** | an assistant that answers questions about the deployment and proposes changes you apply; the model it runs on is set under Models |

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
made. A clean re-run is a fresh workspace, not an archive.

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
assuming a missing key.

`model produced no parseable file blocks` is the catch-all, and the model's own answer —
quoted on that line — says which of several things went wrong. The ones seen in practice:
it did not receive the files it was asked about (a context problem — the task should name
them, by path or by the identifier the file is generated from); or it received them and
says it cannot write that much, which means the work is larger than one delegation's
output budget (`_DEFAULT_CODEGEN_MAX_TOKENS`, 12000) and belongs in several work items.
Rewriting five 10KB fixtures in one item is past it; one item per fixture is not.

A third shape, seen once: the task names two very large files, they are *both* read —
each is inside the per-file allowance — and the model still reports them truncated. The
per-file budget is `prefer_file_bytes`; **nothing caps the total**, so a work item naming
a 30KB source and a 27KB fixture asks for 57KB of preferred content on top of the rest of
the tree. Four sibling items naming smaller files succeeded in the same run. Split by
file size as well as by count when an item names something unusually large.

**An agent cannot regenerate machine output by reasoning.** Snapshot fixtures, golden
files and recorded terminal grids are produced by *running* the code. Given the fixture
and the renderer, a model writes a plausible one — and plausible is wrong: in one run it
rendered a timer as `01:00` where the code emits `00:00`, a keybinding as `[H]` where the
code emits `[unbound]`, and dropped style rows the renderer writes. An approximate
snapshot is worse than the failing test, because it makes a real regression permanent and
invisible. Regenerate those with the tool that produces them (`cargo insta accept` and
its equivalents), in the build, and give agents the work that needs judgement. A provider authentication error there points at a request built
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

- [`docs/packs-and-samples.md`](packs-and-samples.md) — where a deployment's content comes from
- [`docs/configuration.md`](configuration.md) — every environment variable
- [`docs/install.md`](install.md) — standing a deployment up in the first place
- [`docs/delegation.md`](delegation.md) — the delegate / review / rework loop these workers run

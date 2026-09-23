# Workflow packs and sample tenants

Pyrrhula's content lives in two repositories that are not this one, and a deployment is
not finished until both have been pulled in. This document is the whole path for each:
where the content lives, how it reaches a deployment, and how it reaches a tenant.

| Repository | What it holds | Reaches a deployment by |
|---|---|---|
| [`pyrrhula-workflows`](https://github.com/tuturu742/pyrrhula-workflows) | flows, entity schemas, rule systems, tool declarations, behaviour axes, vocabulary overlays | being **pinned and baked into the image**, then synced on boot |
| [`pyrrhula-samples`](https://github.com/tuturu742/pyrrhula-samples) | `.pyr` bundles (a cast, its briefs, its knowledge), per-sample `repos.json` / `mcp.json`, and a README per sample | being **imported into a tenant**, by `scripts/seed_samples.py` or by hand |

Neither repository contains code that Pyrrhula runs. A pack is JSON validated against
declarative manifests, and a `.pyr` is content; rule 9 and D7 are why, and why a pack can
be pinned to a commit and trusted the way a configuration file is.

## Registering workflows

### 1. The content

A plugin repository is a directory with `plugin.json` at its root naming the workflow
packs it provides:

```json
{
  "name": "Pyrrhula core workflows",
  "description": "Tabletop RPG, software development, and enterprise discussion workflows.",
  "workflows": ["rpg", "swdev"]
}
```

Each named workflow is a directory, and each of its subdirectories is one content kind:

```
swdev/
  workflow.json      the workflow's own manifest -- name, vocabulary, capabilities
  schemas/           entity schemas (work_item, character, ...)
  processes/         flows: phases, actors, budgets, gates, prompts
  rule_systems/      how a roll is judged
  tools/             tool declarations a phase may allow
  axes/              behaviour axes a persona can be positioned on
  overlay/           vocabulary overlay -- the domain words for this workflow's label keys
  seed/              starter knowledge sources
```

Authoring rules for each kind live in `AUTHORING.md` in that repository. What matters here
is that all of it is data: the pack loader only ever reads JSON.

### 2. Pinning it to a deployment

`deploy/plugins.json` names the repository and the **exact commit**:

```json
{
  "default": {
    "url": "https://github.com/tuturu742/pyrrhula-workflows",
    "ref": "a0f3307cb3c5956e1ed12ced307c91a2945a0ec2"
  }
}
```

`scripts/fetch_plugins.py` clones that ref into `.plugins/`, and the image build copies
`.plugins/default` to `/app/packs` (`docker/Dockerfile`). The installers run the fetch for
you.

**A pack change is not shipped when it is committed.** It is shipped when the pin names it
*and* the fetch succeeded. For a private repository the fetch needs a token:

```bash
export PYRRHULA_PLUGINS_TOKEN="$(tr -d '\n' < /path/to/secrets/gh_tuturu)"
export PYRRHULA_PLUGINS_STRICT=1     # stale pack -> build failure instead of a warning
python scripts/fetch_plugins.py
```

Without the token the fetch fails, the build uses whatever is cached on disk, and the
install reports success with the previous pack inside it. The script prints a WARNING
naming both refs when that happens; `PYRRHULA_PLUGINS_STRICT=1` turns it into an error,
which is what a CI build should use.

For an install that cannot reach the pinned repository at all, drop pack directories into
`docker/plugins-local/` (`PYRRHULA_PLUGIN_DROP_DIR`) — see the README there.

### 3. Syncing it into the deployment

On boot, the baked default is synced automatically: its workflow manifests become system
workflow templates and its overlays become global vocabulary overlays, both written as
NULL-tenant rows by the admin engine (the app role's RLS forbids writing them, which is
deliberate — deployment-level content is not a tenant's to author).

Additional plugin repositories are an operator action in the admin console, or over its
API:

```bash
# register another provider at a pinned ref, and sync it
curl -X POST "$ADMIN/admin/plugin-repositories" -H 'Content-Type: application/json' \
  -d '{"name":"house-flows","url":"https://github.com/acme/house-flows","ref":"<sha>"}'
curl -X POST "$ADMIN/admin/plugin-repositories/$ID/sync"

# or upload an archive, for an air-gapped deployment
curl -X POST "$ADMIN/admin/plugin-repositories/upload?name=house-flows" \
  --data-binary @house-flows.zip
```

`GET /admin/plugin-repositories` lists what is registered and at which ref; `DELETE`
removes one.

### 4. Loading a pack into a tenant

Syncing publishes deployment-level content. A tenant's own rows — the entity schemas its
sessions validate against, the flows its pickers show, its tools, axes and rule systems —
are created by loading a pack *into a tenant and workspace*:

```python
from core.packs.loader import load_pack
await load_pack(pathlib.Path("/app/packs/swdev"), tenant_id, workspace_id)
```

`scripts/seed_samples.py` does this for each seeded tenant (`rpg` for a tabletop sample,
`swdev` for a software one), which is why a sample tenant has flows the bundle itself
never carried.

Loading is **versioned, not idempotent**: loading the same pack twice leaves v1 and v2 of
every flow active, both in the picker and indistinguishable by name. The seeder archives
every superseded version afterwards, keeping the newest of each key. If you call
`load_pack` yourself in a loop, do the same — an in-flight session resolves its phases
against the definition row it started on, so archive rather than delete.

## Setting up sample tenants

Each sample in `pyrrhula-samples` is a directory holding a `.pyr` bundle and a README that
walks a person through setting it up by hand. Some also carry:

| File | What it declares |
|---|---|
| `repos.json` | repositories to register: source url, build runtime and image, test and build commands, artifact name, and the preview recipe (`preview_image`, `preview_cmd`, `preview_port`, `preview_env`) |
| `mcp.json` | external MCP servers the sample's case depends on — url, allowed tools, per-session budget |
| a server and its manifests | the tool itself, when the sample needs one running (the hagnaryd forensic lab) |

A bundle has never carried a credential or a repository registration: a `.pyr` is content,
and both of those are half configuration and half secret. That is why every sample README
has a step telling you to add your own key.

### The scripted path

```bash
python scripts/seed_samples.py \
  --secrets-dir /path/to/secrets \
  --samples-dir ~/code/pyrrhula-samples \
  --samples hagnaryd-mystery,mice-invaders,loxia=pyrrhula
```

Per tenant this creates the model connections, imports the bundle, binds every persona to
the connection its **role** calls for, seats them in the workspace, registers whatever
`repos.json` and `mcp.json` declare, and loads the pack. It is the scripted form of the
README, not a second path: a rebuild that takes forty clicks does not happen daily.

`slug=sample` names the tenant differently from the sample it borrows: `loxia=pyrrhula`
seeds a tenant called `loxia` from the `pyrrhula` bundle's bench. When the samples
directory has a folder named after the **slug**, its `repos.json` and `mcp.json` are read
from there rather than from beside the bundle — which is how `loxia` brings its own
repository while borrowing someone else's cast.

The script needs the same environment the API has (`PYRRHULA_APP_DATABASE_URL`, the
encryption key), so run it inside the api container or with that environment exported.

### Starting a session in a seeded tenant

`scripts/start_sample_session.py` does what the New Session form does, resolved by name:

```bash
python scripts/start_sample_session.py \
  --tenant loxia --flow investigate_plan_implement_review_merge \
  --supervisor Architect \
  --participants "Staff Dev,Senior Dev,Middle Dev,Junior Dev,QA" \
  --repos loxia --name "Loxia docs" --agenda-file agenda.txt
```

Every purge renumbers every id, so it takes a tenant *slug*, a flow *key* and persona
*names*, and it picks the newest unarchived version of the flow — starting on a
superseded version is how a pack fix that shipped never reaches a session. Binding a
repository also registers its git MCP server, which is what makes `delegate_work_item`
exist for a phase that allows it; a phase that allows the tool with no server registered
simply never sees it.

### The manual path

The READMEs are authoritative for doing it by hand, and each is specific about its own
sample. The shape is the same every time:

1. Sign up, which makes you the tenant's steward.
2. **Settings → Model connections**: add a connection per provider the sample expects.
3. **Settings → Import**: upload the `.pyr`, review what it declares, import it.
4. Bind each persona to a connection (**Agents**).
5. If the sample has a `repos.json`: register its build runtime under **Repos → Build
   runtimes**, then the repository itself with its token, test/build commands and preview
   recipe.
6. If the sample has an `mcp.json`: start the server it names, then register it under
   **Settings → MCP servers**.

Step 4 is the one worth checking twice. An imported persona with no connection produces a
session that starts and then fails on its first turn, which reads as a broken flow rather
than an unfinished setup.

### Running a sample a second time

A workspace keeps what its sessions made. Characters, work items, and every other entity
are workspace-scoped, and so is a persona's binding to one — only the transcript belongs
to the session (see "What a session carries" in
[`docs/agent-guide.md`](agent-guide.md)). That is the right default for a table that meets
again, and it means **re-running a sample from the top is not what a second session does**:
the second campaign opens with the first one's characters already in context, and its
players will read them and decline to roll new ones.

Archiving the first session does not help — archiving hides a session, it does not remove
what the session made. For a clean re-run, purge and reseed the tenant:

```bash
python -m core.tenancy.purge --tenant karsh-vale          # dry run first
python -m core.tenancy.purge --tenant karsh-vale --yes
python scripts/seed_samples.py --secrets-dir … --samples-dir … --samples karsh-vale
```

See [`docs/operations.md`](operations.md) for what that purge does and where to run it.

## Where to fix what

| Wrong thing | Repository |
|---|---|
| a flow, schema, rule system, tool, axis, or overlay | `pyrrhula-workflows` — then bump `deploy/plugins.json` and rebuild |
| a sample's content, its setup steps, its repository or MCP declarations | `pyrrhula-samples` |
| the platform: the loader, the installers, the API, the UI | this repository |

Never fix content by editing rows in a running deployment. The daily rebuild
(`docs/runbook-daily.md`) deletes that deployment, and a fix that lived only there goes
with it.

## See also

- `docs/runbook-daily.md` — the purge-and-rebuild loop these two repositories feed
- `docs/portability.md` — what a `.pyr` carries and what it deliberately does not
- `docs/previews.md` — the preview recipe `repos.json` can declare
- `docs/mcp.md` — registering external tool servers
- `AUTHORING.md` in `pyrrhula-workflows` — how to write each content kind

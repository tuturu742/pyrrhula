# Workflow packs and sample tenants

Pyrrhula's content lives in two repositories that are not this one: the workflow packs a
deployment builds in, and the samples — redistributable example workspaces you may import
or ignore. This document is the whole path for each: where the content lives, how it
reaches a deployment, and how it reaches a workspace.

| Repository | What it holds | Reaches a deployment by |
|---|---|---|
| [`pyrrhula-workflows`](https://github.com/tuturu742/pyrrhula-workflows) | flows, entity schemas, rule systems, tool declarations, behaviour axes, vocabulary overlays | being **pinned and baked into the image**, then synced on boot |
| [`pyrrhula-samples`](https://github.com/tuturu742/pyrrhula-samples) | `.pyr` bundles (a cast, its briefs, its knowledge), per-sample `repos.json` / `mcp.json`, and a README per sample | being **imported into a workspace** through the UI, following each sample's README |

Neither repository contains code that Pyrrhula runs. A pack is JSON validated against
declarative manifests, and a `.pyr` is content; rules 9 and 10 are why, and why a pack can
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
  workflow.json the workflow's own manifest -- name, vocabulary, capabilities
  schemas/           entity schemas (work_item, pull_request, build, ...)
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
    "ref": "<commit sha>"
  }
}
```

`scripts/fetch_plugins.py` clones that ref into `.plugins/`, and the image build copies
`.plugins/default` to `/app/packs` (`docker/Dockerfile`). The installers run the fetch for
you.

**A pack change is not shipped when it is committed.** It is shipped when the pin names it
*and* the fetch succeeded:

```bash
export PYRRHULA_PLUGINS_STRICT=1     # stale pack -> build failure instead of a warning
python scripts/fetch_plugins.py
```

When the fetch fails, the build uses whatever is cached on disk, and the
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
curl -X POST "$ADMIN/admin/plugin-repositories/upload" \
  -F name=house-flows -F file=@house-flows.zip
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

Choosing a workflow for an organization (**Workflows** in the UI, or the admin console's
workflow setting for a tenant) does this, which is why a workspace has flows the bundle it
imported never carried.

Loading is **versioned, not idempotent**: loading the same pack twice leaves v1 and v2 of
every flow active, both in the picker and indistinguishable by name. Applying a workflow
is stamped per workspace, so re-applying the same pinned pack loads nothing; a new pin
loads a new version, and nothing archives the superseded one for you — archive it from
the flow's own page (`POST /process-definitions/{id}/archive`) rather than deleting it,
because an in-flight session resolves its phases against the definition row it started on.

## Setting up sample tenants

Each sample in `pyrrhula-samples` is a directory holding a README and, for all but `loxia`
(which borrows another sample's cast), a `.pyr` bundle, that
walks a person through setting it up **in the product** — every step is something you do in
the UI, and no sample asks you to run a script. Some also carry:

| File | What it is |
|---|---|
| `repos.json` | the repository registration the README walks you through, as data: source url, build runtime and image, test and build commands, artifact name, and the preview recipe. Read it; the fields map one-to-one onto the **Repos** form |
| `mcp.json` | the external MCP servers the README has you register on the workspace — url, allowed tools, per-session budget, options |
| a server and its manifests | the tool itself, when the sample needs one running (the hagnaryd forensic lab). Standing that up is the one step outside the product |

A bundle has never carried a credential or a repository registration: a `.pyr` is content,
and both of those are half configuration and half secret. That is why every sample README
has a step telling you to add your own key.

### Setting one up

The READMEs are authoritative, and each is specific about its own sample. The shape is the
same every time:

1. Sign up, which makes you the organization's steward.
2. **Workflows**: choose the workflow the sample was authored under, *before* importing —
   it brings the vocabulary and the personality axes the cast uses.
3. **Personas → Model profiles**: add a connection per provider the sample expects, with
   your own key.
4. On the workspace, **Export / import**: upload the `.pyr`, read the inspection verdict,
   import it.
5. **Personas**: open each imported persona and set its connection.
6. If the sample has a `repos.json`: register its build runtime under **Repos → Build
   runtimes**, then the repository itself with its token, test and build commands, and
   the preview recipe — the same fields, in the same names.
7. If the sample has an `mcp.json`: start the server it names, then register it under the
   workspace's **MCP servers** with the key, url, tools, budget and options it lists.
8. Start a session from the roster and agenda the README gives.

Step 5 is the one worth checking twice. An imported persona with no connection produces a
session that starts and then fails on its first turn, which reads as a broken flow rather
than an unfinished setup.

**The order of steps 2 and 4 matters** for anything a pack and a bundle both define. Both
register by key; whichever loads second wins. Choosing the workflow first and importing
second leaves the bundle's version standing — which is what a sample wants: karsh-vale
ships a `randomizer` tool bound to its own Basic Fantasy rule system, and the `rpg` pack
ships the same key bound to the generic d20 system. Import first and apply the workflow
afterwards, and every roll silently resolves under the wrong rules — the tool still works
and still writes an honest hash-chained record. If you change a workspace's workflow later,
import the bundle again: its rule systems and tools upsert in place, so the pack's
versions are replaced once more, while resident personas are skipped and colliding
content forks.

### Running a sample a second time

A workspace keeps what its sessions made. Characters, work items, and every other entity
are workspace-scoped, and so is a persona's binding to one — only the transcript belongs
to the session. That is the right default for a table that meets
again, and it means **re-running a sample from the top is not what a second session does**:
the second campaign opens with the first one's characters already in context, and its
players will read them and decline to roll new ones.

Archiving the first session does not help — archiving hides a session, it does not remove
what the session made. For a clean re-run, **archive the workspace and import the bundle
into a fresh one**: the app role holds no DELETE grant on the tables that carry history, so
the product offers archiving, not erasure. Deleting a tenant outright is the operator's
`core.tenancy.purge` CLI, described in [`docs/operations.md`](operations.md).

## Where to fix what

| Wrong thing | Repository |
|---|---|
| a flow, schema, rule system, tool, axis, or overlay | `pyrrhula-workflows` — then bump `deploy/plugins.json` and rebuild |
| a sample's content, its setup steps, its repository or MCP declarations | `pyrrhula-samples` |
| the platform: the loader, the installers, the API, the UI | this repository |

## See also

- `docs/portability.md` — what a `.pyr` carries and what it deliberately does not
- `docs/entities-and-state-machines.md` — the schemas a pack ships under `schemas/`, and the
  state machines on them
- `docs/previews.md` — the preview recipe `repos.json` can declare
- `docs/mcp.md` — registering external tool servers
- `AUTHORING.md` in `pyrrhula-workflows` — how to write each content kind

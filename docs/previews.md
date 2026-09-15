# Preview environments

A preview takes a repo's build artifact and runs it somewhere a human can open it, behind
a share link that expires. It is the "look at the thing that was just built" half of the
delegation pipeline — the build half is in [exec-engines.md](exec-engines.md).

## Before a preview exists: the build

A preview serves a **build artifact**, so the repo has to produce one. On the repo's page
(**Repos → the repo → Edit**), two fields together create the build step:

| Field | Example |
|---|---|
| **Build command** | `npm run build && tar czf dist.tgz -C dist .` |
| **Artifact file** | `dist.tgz` |

**Both are required.** With either missing there is no build step at all, no artifact is
uploaded, and *Deploy preview* has nothing to serve — which looks the same as a preview
that failed.

The build command runs in the repo's exec environment after the tests pass (or immediately,
if no test command is set), from the repo root. Whatever file you name is uploaded to the
artifact store and is what the preview container downloads.

A `.tar.gz` is the shape the default static server expects (it unpacks it and looks for
`index.html`). A recipe can take any shape it knows how to run.

## What a preview runs

By default: your artifact, extracted, served as a static site. That covers a web build —
a Godot HTML5 export, a Vite bundle — and nothing else. If your project is a server, it
needs a recipe.

Three layers decide, most specific first:

1. **Repo settings** — the fields on the repo, editable in the UI. An operator override,
   so a broken recipe is fixable without a commit and a rebuild.
2. **`pyrrhula-preview.json` in the repo**, at the ref being previewed — the recipe living
   with the code it describes, versioned alongside it.
3. **The platform default** — the static server. A repo that ships no manifest and sets no
   overrides behaves exactly as it always did.

## The manifest

```json
{
  "image": "docker.io/library/node:22-slim",
  "cmd": "node server.js",
  "port": 3000,
  "env": { "NODE_ENV": "production" }
}
```

| Field | Meaning |
|---|---|
| `image` | The container the preview runs in. Needs whatever your command needs; nothing is installed for you. |
| `cmd` | What to run, in the extracted artifact directory. Runs under `sh -lc`, so pipelines and `&&` work. Omit it for the static server. |
| `port` | What your process listens on. The platform publishes it and routes the share link to it. Default 8080. |
| `env` | Extra environment. Cannot set `PYR_ARTIFACT_URL` or `PYR_ARTIFACT_TOKEN`. |

A malformed manifest **refuses the preview** rather than falling back to the static
server: an author who wrote a recipe and silently got a directory listing has no way to
tell their file was ignored.

## What a recipe cannot change

Fetching the artifact and unpacking it stays with the platform, whatever the recipe says:

- the artifact URL and a **scoped, expiring read token** for exactly one artifact,
- extraction guarded by `filter="data"` (PEP 706), which refuses absolute paths, traversal
  and special files.

Your command runs *after* that, with the working directory set to the extracted artifact.
This is why `env` cannot set the two `PYR_ARTIFACT_*` variables — they are how the
container reaches its own build, and a recipe that could rewrite them could point the
container at a different one.

Your command is still ordinary tenant-authored code in a container, exactly like
`build_cmd` already is, under the same network policy.

## Examples

**A static web build** (the default — no manifest needed). Produce an artifact with
`index.html` at its root and open the share link.

**A Node server:**

```json
{ "image": "docker.io/library/node:22-slim", "cmd": "node dist/server.js", "port": 3000 }
```

**A Python app** — note that the default image has no pip packages, so vendor them into the
artifact or use an image that has them:

```json
{ "image": "docker.io/library/python:3.12-slim", "cmd": "python -m app", "port": 8000 }
```

**A game with a backend** is not yet one preview: a recipe starts one container. Run the
backend as its own repo and preview, and point the client at it.

## Lifetime

Previews expire (`PYRRHULA_PREVIEW_TTL_SECONDS`, capped by
`PYRRHULA_PREVIEW_MAX_TTL_SECONDS`) and are reaped. Starting a preview twice for one repo
converges on a single container rather than leaking a second.

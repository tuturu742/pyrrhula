# Installing Pyrrhula

Two supported deployment targets, one entry point:

```bash
./install.sh compose   # docker or podman on one machine -- smallest footprint
./install.sh k8s       # a Kubernetes cluster -- built against k3s
```

Add `--check` to any target to verify prerequisites without changing anything.

Both build the images from this checkout. A third path skips the build and pulls the
images of a published release instead — no checkout, no toolchain, one compose file:

```bash
curl -fsSL https://raw.githubusercontent.com/tuturu742/pyrrhula/main/deploy/installers/release.sh | sh
```

See [release images](#release-images-no-checkout-no-build). Which to choose: pull a
release to run the product, build from source to change it.

## Prerequisites

Both installers build the images from source inside containers, so the host needs no
Python or Node toolchain — the image builds bring their own (`uv`, Node 22, `pnpm`).
The release path builds nothing at all: it needs only a container engine, `curl` and
`openssl`.

**compose**

- Docker 24+ with the Compose plugin, or Podman 4+ with `podman-compose`. For delegated
  coding agents, the engine's API socket: rootless Podman needs
  `systemctl --user enable --now podman.socket`; Docker needs `/var/run/docker.sock`
  readable by the user running the installer.
- `git` and `openssl` (the installer generates the secrets with it).
- About 4 GB of RAM for the stack, plus 2–3 GB per retrieval model you download, and
  10 GB of disk for images, the database and the model cache. A local model server
  (Ollama) is extra, and optional.

**k8s**

- `kubectl` with access to a cluster that has a default storage class and an ingress
  controller. Built and tested against k3s, where one `curl` provides both.
- Docker or Podman on the machine running the installer, to build the images, and either
  `sudo` (the images are imported into k3s's containerd) or a registry the cluster can
  pull from (`PYRRHULA_K8S_REGISTRY`, see `deploy/k8s/README.md`).
- `helm`, only for the optional `--with-dashboard` (Headlamp).

**To develop on the code** rather than run it: Python 3.12 and [`uv`](https://docs.astral.sh/uv/),
Node 22 with `pnpm`, and a PostgreSQL 16 with pgvector plus a Redis 7 for the test suite.
`CONTRIBUTING.md` has the commands.

## Single-tenant or multi-tenant

Installs **single-tenant** unless you say otherwise:

```bash
./install.sh compose                  # one organization (default)
./install.sh compose --multi-tenant   # several, each named at login
```

Single-tenant means nobody types an organization name to sign in — the right shape for
one person or one team. Multi-tenant means every sign-in names its organization.

**A single-tenant install needs no credentials from the installer.** Open the URL, sign
up, and you are the owner of the deployment's one organization *and* its platform
admin — one account, one UI: the admin pages sit under an **App settings** tab in
your own navigation (**Admin → Models** in this guide means that tab). There is no second
generated account to log in as. (One is still bootstrapped as break-glass, printed at
the end of the install; you should not need it.)

In **multi-tenant** mode the platform admin stays a separate account, because there it is
a separate person: the one running the box for organizations they are not a member of.
Owning a tenant grants nothing over the deployment or over anyone else's tenant.

It is a flag over the same multi-tenant core, never a different build, so **the choice
is not permanent**: rerun the installer with the other flag and it switches. Nothing is
migrated, no data changes, and every organization stays reachable either way — the flag
only decides what happens when a login arrives without naming one.

One thing to know about growing: a single-tenant deployment that signs up a *second*
organization can no longer infer which one a header-less login means, and says so
instead of guessing. Either name the organization at login, switch to `--multi-tenant`,
or pin one with `PYRRHULA_DEFAULT_TENANT_SLUG`.

| | compose | k8s |
|---|---|---|
| Good for | trying it out, small self-host | dev/test on a cluster, k8s shops |
| Prereqs | see below | see below |
| Agents' code runs in | sibling containers (engine socket) | one-shot **Jobs** (isolated namespace) |
| Cost | your machine | your machine/cluster |
| Time to first login | ~5 min (image build) | ~10 min |

Every target ends at the same place: open the printed URL, **Register** (the first
account creates your organization + workspace), follow the setup checklist — add a model
connection, create a starter team, launch a session.

---

## The retrieval models

Pyrrhula runs two models itself: one embeds text for search, one reranks the results. They
are ~3GB together and live in a cache volume shared by the api and the worker.

**The installer does not download them, and does not choose them for you.** It used to,
and that was the slowest part of an install by a wide margin — several gigabytes spent
before you had seen a single screen, on a model nobody had picked. Which models a
deployment runs is a decision its operator makes, so it is made where decisions are made:

**Admin → Models**, on your first login:

- **Choose** the embedding and reranking models this deployment uses. Deployment-level,
  not per tenant — every tenant's vectors live in one column of one width, so this cannot
  coherently differ between them.
- **Download from Hugging Face** — a background job; the sizes on that page grow as it
  runs. Safe to press twice.
- **Upload cache archive** — for a deployment with no route to `huggingface.co`. On a
  machine that has one, with Python and `pip install huggingface_hub`:

  ```bash
  python -c "from huggingface_hub import snapshot_download as d; d('BAAI/bge-m3'); d('BAAI/bge-reranker-v2-m3')"
  tar czf cache.tgz -C ~/.cache/huggingface hub
  ```

  The first line downloads into `~/.cache/huggingface/hub` (substitute the models you
  chose); the second archives that `hub` directory — `-C` changes into its parent and
  `hub` is what gets archived, so the command is written exactly as shown, not with
  `hub` appended to the path. Upload `cache.tgz` on the Models page. An archive of the
  directory's contents (`tar czf cache.tgz -C ~/.cache/huggingface/hub .`) is accepted
  too: the upload looks for the `models--<org>--<name>` directories wherever they sit.

Until you do, the deployment is *installed and working* — it just cannot answer a
semantic query. Knowledge still ingests, chunks and stores; sessions still run. The
install check at the end of every installer says which models are present, and the app
tells you where to go rather than stalling: the runtime never fetches a model
mid-request, because an unauthenticated hub check has no timeout and has been seen
wedging the API's event loop for thirteen minutes at idle CPU.

## Does it work? The installer answers that

Every installer ends by running the same check, and **fails the install if it does not
pass**. It is not the readiness probe — that one proves the API reached the database.
This one drives a document through the whole loop: a blob write, a job on the queue, the
*worker* claiming and running it, a parse, rows in the database. A worker that OOMs on
its first job, a queue whose leases were never reclaimed, a blob volume mounted
read-only — all of those look healthy to a readiness probe and fail here.

It needs no workflow pack and no model, so it means the same thing on every install,
including one that will only ever run `swdev`. To re-run it later:

```bash
podman exec pyrrhula_api_1 python /app/deploy-smoke.py          # compose
kubectl -n pyrrhula exec deploy/pyrrhula-api -- python /app/deploy-smoke.py   # k8s
```

## compose (docker / podman)

```bash
git clone <repo> && cd Pyrrhula
./install.sh compose
```

What it does: detects your engine, writes `.env` with five generated secrets
(backed up to `~/.config/pyrrhula/compose.env.bak`), wires the engine socket for
delegated coding agents, `compose up -d --build`, waits for health — reporting what the
stack is doing if it takes more than 45 seconds rather than sitting silent — and prints
the URL. It does not download the retrieval models: that choice is the platform admin's,
under **Admin → Models**.

- **UI** http://localhost:5173 · **platform admin** lives in the same UI: sign in with
  organization `admin`. The installer generates the account and **prints the email and
  password when it finishes**; they are also in `.env`
  (`PYRRHULA_ADMIN_EMAIL`/`PYRRHULA_ADMIN_PASSWORD`). Change the password in the app
  after first login — the account is created once, so editing `.env` afterwards does not
  rotate it. More admins can be added from the console.
- **Rootless podman**: enable the socket first —
  `systemctl --user enable --now podman.socket` (the installer warns if missing;
  without it delegation falls back to no-environment mode).
- **Local models**: uncomment the `ollama` service in
  `docker/compose.selfhost.yml`, or point connections at any cloud key.
- **Upgrade**: `git pull && ./install.sh compose` (compose rebuilds; the migrate
  one-shot runs Alembic before api/worker start).
- **Skip the build**: `./install.sh compose --from-registry` runs the same stack from
  the published images — minutes instead of a first build. Add `=VERSION` to pin one
  (`--from-registry=0.1.0-rc1`). Everything else on this page still applies; the
  difference is that you are running the tagged code rather than your working tree.
- **Offline model loads**: `PYRRHULA_HF_OFFLINE` defaults to `1`, so the runtime never
  reaches Hugging Face on its own; the admin-console download lifts that for its one
  fetch. This is not a preference — an unauthenticated hub check has no timeout and can
  hang *inside* the in-process model load, wedging the event loop (seen on the k8s stack
  as NotReady for 13+ minutes at idle CPU). Set it to `0` only if you want the old lazy
  in-request fetch back.
- **TLS**: terminate in front of the web port with any proxy (Caddy example in
  `docs/self-host.md`).

## release images (no checkout, no build)

For running the product rather than working on it. Three images are published per
release to GitHub Container Registry, and one compose file wires them together:

| image | what it runs |
|---|---|
| `ghcr.io/tuturu742/pyrrhula` | api, worker and the migration one-shot — one image, the entrypoint selects |
| `ghcr.io/tuturu742/pyrrhula-web` | the built UI behind nginx |
| `ghcr.io/tuturu742/pyrrhula-searxng` | agent web search, with this platform's settings baked in |

linux/amd64. They are public: no `docker login`.

```bash
curl -fsSL https://raw.githubusercontent.com/tuturu742/pyrrhula/main/deploy/installers/release.sh | sh
curl -fsSL .../release.sh | sh -s -- 0.1.0-rc1          # a specific release
```

Everything lands in `./pyrrhula` (`PYRRHULA_DIR` to choose). The script generates the
same secrets the compose installer does, pulls, migrates, waits for the stack and prints
the URL and the admin login. Rerunning it upgrades in place — the `.env` is kept.

**Without the script**, if you would rather read what you run:

```bash
curl -fsSLO https://raw.githubusercontent.com/tuturu742/pyrrhula/v0.1.0-rc1/docker/compose.release.yml
cat > .env <<EOF
PYRRHULA_VERSION=0.1.0-rc1
PYRRHULA_POSTGRES_PASSWORD=$(openssl rand -hex 24)
PYRRHULA_APP_DB_PASSWORD=$(openssl rand -hex 24)
PYRRHULA_JWT_SECRET=$(openssl rand -base64 48)
PYRRHULA_ENCRYPTION_KEY=$(openssl rand -base64 32)
PYRRHULA_ADMIN_EMAIL=admin@example.com
PYRRHULA_ADMIN_PASSWORD=$(openssl rand -hex 12)
EOF
chmod 600 .env
docker compose -p pyrrhula -f compose.release.yml up -d
```

Two things differ from a source install, both deliberate:

- **The first start is online.** The ~2.2 GB retrieval model is too large to put in an
  image, so a pulled deployment begins with an empty cache volume and fills it once.
  `PYRRHULA_HF_OFFLINE` therefore defaults to `0` here rather than `1`. Set it to `1`
  after the first successful start if the host should never reach Hugging Face again.
  Until the model is cached the stack runs and accepts turns but retrieves nothing, so
  give it those few minutes before judging a first session.
- **Hand-placed workflow packs go in a volume**, not a directory beside the compose
  file — a file you downloaded on its own has nothing beside it. Use
  `docker cp <pack> pyrrhula_api_1:/app/plugins-local/`.

`GET /health` on the api reports the version it is running, which is the only reliable
way to tell what a pulled deployment actually is:

```bash
curl -s localhost:5173/api/health     # {"status":"ok","version":"0.1.0rc1"}
```

## k8s (Kubernetes)

No cluster yet? Single-node [k3s](https://k3s.io) is one command:

```bash
curl -sfL https://get.k3s.io | sh -
sudo install -D -m 600 -o $USER -g $USER /etc/rancher/k3s/k3s.yaml ~/.kube/config
export KUBECONFIG=~/.kube/config
```

Then:

```bash
./install.sh k8s --with-dashboard   # dashboard (Headlamp) is optional
```

What it does: prereq checks, optional Headlamp install (login token saved to
`~/.config/pyrrhula/headlamp-token.txt`; open with
`kubectl -n kube-system port-forward svc/headlamp 8085:80`), then the dev-up flow —
builds both images, imports them into k3s's containerd (**needs sudo** for
`k3s ctr images import`), generates `deploy/k8s/overlays/dev/secrets.env`, applies
the kustomize overlay, and runs the migration Job.

- **UI** http://pyrrhula.localhost (k3s traefik; `*.localhost` needs no DNS setup).
- **Platform admin** in the same UI: sign in with organization `admin`.
- Delegated coding agents run as Jobs in `pyrrhula-envs`, restricted by
  NetworkPolicy to DNS + the api's git endpoint + the internet.
- **Retrieval models** are not downloaded at install: fetch them under **Admin →
  Models** (or upload a cache archive on a cluster with no route to Hugging Face).
  Until then semantic queries are refused, plainly.
- **Upgrade**: `git pull && ./install.sh k8s`.
- **Multi-node**: the api and worker share ReadWriteOnce volumes, so a `podAffinity`
  keeps them on one node. Spreading them needs ReadWriteMany storage — see below.
- **Engine-only mode** (app stays on compose, agents execute on a cluster) and
  **other clusters** (non-k3s: registry images, ingress class, storage class, the
  single-node constraint): `deploy/k8s/README.md`, *Other clusters*, with a copyable
  overlay at `deploy/k8s/overlays/cluster`.

## After any install

1. **Register** — the first account creates your organization; the setup checklist
   walks through the rest.
2. **Model connections** (**Personas → Model profiles**): paste an Anthropic / OpenAI /
   Gemini / any OpenAI-compatible key, or point at an Ollama instance. Keys are
   sealed with AES-256-GCM at rest.
3. **Usage limits** (**Organization** page → "Daily usage limits"): daily token caps per
   organization / connection / persona / user — the platform-side backstop for
   provider bills. 0 = unlimited; at the cap, sessions pause and calls return 429
   until midnight UTC.
4. **Repos + engines** (Repos page): register a repo for software-development
   flows; pick where agent environments run if the deployment declares several
   engines.

## Workflow packs

The specialised workflows (Tabletop RPG, Software Development) are content, and live in
the repository pinned by `deploy/plugins.json`. The installers fetch it; the image bakes
the result in.

If that repository is not reachable from where you are installing — you are offline —
the install still completes and the deployment falls back to the built-in `Default`
workflow. You will see a note saying so, and the install never prompts for git
credentials: an unreachable pin fails fast rather than hanging an unattended install on a
username prompt.

### Adding packs without git

Neither of these needs git access, and both survive restarts.

**Upload.** Admin console → *Plugin repositories* → *Choose archive*: a `.zip` or `.tar.gz`
whose root holds `plugin.json`. An archive with a single wrapping directory (GitHub's
"Download ZIP", or `tar czf` of a checkout) is unwrapped for you. Works on every target
with no redeploy:

```bash
# $TOKEN: a platform admin's login (POST /auth/login with X-Pyrrhula-Tenant: admin)
curl -sS -X POST http://localhost:8000/admin/plugin-repositories/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F name=my-workflows -F file=@my-workflows.zip
```

**Drop directory.** Put each pack — the directory containing `plugin.json` — into
`docker/plugins-local/` and restart; everything there is validated and registered at boot. The path inside the container is `PYRRHULA_PLUGIN_DROP_DIR`
(default `/app/plugins-local`), mounted read-only, so packs stay yours: remove one by
deleting the directory and restarting, not from the console. On Kubernetes nothing is
mounted there by default — see `deploy/k8s/README.md` for the ConfigMap/PVC variant, or
just use the upload.

Uploaded packs can be removed again from the console; the built-in and pinned-default
repositories cannot.

## MCP servers and the assistant model

A clean install attaches **no external MCP servers** and configures **no assistant
model** — both are yours to choose. `docs/mcp.md` covers attaching a server (the two
halves: making it reachable, then granting specific tools to a workspace), the built-in
`pyrrhula://` tooling that is not an external server, and setting a model for the
workspace assistant.

## Choosing the retrieval models

The short version is above: pick them in **Admin → Models** after installing. This
section is the detail — what the choice means. There is no environment variable for
it: the console is the one place, and a change applies on the next restart of the api
and worker.

The defaults are `local/BAAI/bge-m3` (1024-wide vectors) and
`local/BAAI/bge-reranker-v2-m3`; any sentence-transformers model works in either slot.

Those particular models are the built-in default because they are multilingual,
permissively licensed (MIT and Apache-2.0) and run acceptably on CPU. They are a starting
point, not a recommendation: a smaller model is faster and cheaper to host, a
domain-specific one may retrieve better on your content, and a deployment that never
searches non-English text has no reason to pay for multilingual weights.

Two rules when changing them:

- **The dimension must match the model.** The width you enter is the deployment's
  declared truth and is checked against the loaded model at startup, so a mismatch
  fails on boot rather than returning nothing at query time.
- **Existing vectors are not migrated.** Embeddings from a different model are not
  comparable; changing the embedding model on a deployment that already has knowledge
  orphans what is stored, and that content has to be re-indexed. Decide before you
  ingest anything, if you can — which is the other reason this choice is the first
  screen a new admin sees.

The reranker is safe to change or disable at any time — it re-ranks results and stores
nothing. Platform admins can see and change both from the admin console after install.

## Upgrading

Migrations are forward-only and run automatically, so an upgrade is: pull, rebuild,
restart.

- **compose**: `git pull && ./install.sh compose` — the installer is idempotent (your
  `.env` is kept; secrets are only generated on first run) and the one-shot `migrate`
  container applies any new migrations before the api starts.
- **k8s**: `git pull && ./install.sh k8s` — rebuilds the images, re-applies the
  overlay, and waits for the migration Job.
- **release images**: rerun the installer with the newer version
  (`curl -fsSL .../release.sh | sh -s -- 0.1.0`), or edit `PYRRHULA_VERSION` in `.env`,
  then `docker compose -p pyrrhula -f compose.release.yml pull && ... up -d` and
  `... run --rm --no-deps migrate`. Run that migration *from the new image*: `up` leaves
  an already-exited one-shot alone, which would put new code on an old schema.

Downgrading is not supported: some migrations carry data transformations whose
`downgrade()` exists for development only. Take a backup before upgrading
(`deploy/k8s/README.md` § Backups) — restoring the dump *is* the rollback.

## The one thing to never lose

Each install generates a `PYRRHULA_ENCRYPTION_KEY` that seals every stored
credential (model keys, repo tokens). Losing it means re-entering them all.

| target | where it lives | backup |
|---|---|---|
| compose | `.env` | `~/.config/pyrrhula/compose.env.bak` |
| release images | `./pyrrhula/.env` | `~/.config/pyrrhula/release.env.bak` |
| k8s | `deploy/k8s/overlays/dev/secrets.env` | make one (the installer prints a reminder) |

## Troubleshooting

- **compose: installer aborts with "a previous install's data exists"** — you have an
  old install's postgres volume but a freshly generated `.env`; postgres only applies
  passwords on first init, so this combination can never authenticate. Restore the
  old `.env` (backup in `~/.config/pyrrhula/`) to keep the data, or wipe with the
  printed `down -v` command to start over.
- **compose (podman): every outbound request from containers times out / DNS fails**
  — podman's aardvark-dns can go stale when the host's resolver setup changes (seen
  after installing k3s on the same machine). `pkill aardvark-dns` — it respawns on
  the next container start with fresh upstream config.
- **compose: "no engine socket" warning** — delegated coding agents need the
  docker/podman API socket; rootless podman: `systemctl --user enable --now podman.socket`,
  then rerun the installer.
- **k8s: pods ImagePullBackOff for `localhost/pyrrhula*`** — the `k3s ctr images
  import` step didn't run (it needs sudo); rerun `./install.sh k8s`.
- **k8s: api pod NotReady for many minutes at idle CPU** — a hung online HF-hub
  check inside the embedding-model load; the pods always run offline, and a *partial*
  cache (an interrupted admin-console download) fails loudly on restart — delete the
  cache PVC contents and download again.
- **k8s: engine declarations from outside the cluster** — use the host's LAN IP
  for `api_base`, not `host.containers.internal` (link-local, doesn't route).
- **Anything model-shaped hangs or errors** — check the connection's key and the
  usage limits page before debugging deeper; a hit cap pauses sessions by design.

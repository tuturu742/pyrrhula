# Installing Pyrrhula

Three supported deployment targets, one entry point:

```bash
./install.sh compose   # docker or podman on one machine  -- smallest footprint
./install.sh k8s       # a Kubernetes cluster              -- built against k3s
./install.sh aws       # AWS ECS Fargate via Terraform     -- cloud demo stack
```

Add `--check` to any target to verify prerequisites without changing anything.

| | compose | k8s | aws |
|---|---|---|---|
| Good for | trying it out, small self-host | dev/test on a cluster, k8s shops | a shareable cloud demo |
| Prereqs | docker or podman (+compose) | kubectl + a cluster (k3s: one curl) | AWS account, terraform/tofu, AWS CLI |
| Agents' code runs in | sibling containers (engine socket) | one-shot **Jobs** (isolated namespace) | one-shot **Fargate tasks** |
| Cost | your machine | your machine/cluster | ~$115/mo at defaults |
| Time to first login | ~5 min (image build) | ~10 min | ~25 min (RDS creation) |

Every target ends at the same place: open the printed URL, **Sign up** (first signup
creates your organization + workspace), follow the setup checklist — add a model
connection, create a starter team, launch a session.

---

## The retrieval models

Pyrrhula runs two models itself: one embeds text for search, one reranks the results. They
are ~3GB together and live in a cache volume shared by the api and the worker.

The installer downloads them at the end of a first install, because the alternative is
that somebody's first knowledge query stalls for several minutes. **It is no longer
required.** Skip it with:

```bash
PYRRHULA_SKIP_MODEL_DOWNLOAD=1 ./install.sh compose   # or: k8s
```

Then get them whenever you like, from **Admin → Models**:

- **Download from Hugging Face** — a background job; the sizes on that page grow as it
  runs. Safe to press twice.
- **Upload cache archive** — for a deployment with no route to `huggingface.co`. On a
  machine that has one, fetch the models, then
  `tar czf cache.tgz -C ~/.cache/huggingface hub` and upload that file.

Nothing here is required for correctness: a model that is missing is fetched the first
time something embeds. That first call is simply slow, and on an air-gapped box it fails
instead — which is what the upload path is for.

## compose (docker / podman)

```bash
git clone <repo> && cd Pyrrhula
./install.sh compose
```

What it does: detects your engine, writes `.env` with five generated secrets
(backed up to `~/.config/pyrrhula/compose.env.bak`), wires the engine socket for
delegated coding agents, `compose up -d --build`, waits for health, prints the URL.

- **UI** http://localhost:5173 · **platform admin** lives in the same UI: sign in with
  organization `admin`. The installer generates the account and **prints the email and
  password when it finishes**; they are also in `.env`
  (`PYRRHULA_ADMIN_EMAIL`/`PYRRHULA_ADMIN_PASSWORD`). Change the password in the app
  after first login — the account is created once, so editing `.env` afterwards does not
  rotate it. More admins can be added from the console. The legacy token console on
  http://localhost:8100 still works (token: `grep ADMIN_TOKEN .env`) but is deprecated.
- **Rootless podman**: enable the socket first —
  `systemctl --user enable --now podman.socket` (the installer warns if missing;
  without it delegation falls back to no-environment mode).
- **Local models**: uncomment the `ollama` service in
  `docker/compose.selfhost.yml`, or point connections at any cloud key.
- **Upgrade**: `git pull && ./install.sh compose` (compose rebuilds; the migrate
  one-shot runs Alembic before api/worker start).
- **TLS**: terminate in front of the web port with any proxy (Caddy example in
  `docs/self-host.md`).

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
the kustomize overlay, runs the migration Job, and flips the pods to offline
embedding mode once the model cache is warm.

- **UI** http://pyrrhula.localhost (k3s traefik; `*.localhost` needs no DNS setup).
- **Admin console** `kubectl -n pyrrhula port-forward deploy/pyrrhula-admin 8100:8100`.
- Delegated coding agents run as Jobs in `pyrrhula-envs`, restricted by
  NetworkPolicy to DNS + the api's git endpoint + the internet.
- **First boot** downloads the 2.2 GB embedding model into the cache volume — the
  first assistant/knowledge call is slow once, then never again.
- **Upgrade**: `git pull && ./install.sh k8s`.
- **Engine-only mode** (app stays on compose, agents execute on a cluster) and
  **other clusters** (non-k3s: push images to a registry, adjust image refs):
  `deploy/k8s/README.md`.

## aws (ECS Fargate)

> **Beta path.** The Terraform is complete and has run against a live account, but it
> is not re-verified every release the way compose and k8s are. Expect to read the
> plan before applying, and file an issue for anything that drifts.

```bash
aws configure          # or SSO -- any credentials that can create VPC/RDS/ECS/IAM
./install.sh aws
```

What it does: `terraform apply` (VPC, RDS Postgres 16 + pgvector, ElastiCache,
EFS, ECR, ALB, Secrets Manager — five secrets generated and injected by ARN, never
in task definitions), builds and pushes both images to ECR, runs the migration
task, prints the ALB URL.

- Delegated coding agents run as one-shot Fargate tasks, isolated by security
  group to the api's git endpoint + the internet.
- **Admin console** is off by default; set `admin_cidrs = ["<your-ip>/32"]` in
  `deploy/aws/variables.tf` (or a tfvars file) and re-apply.
- **Upgrade**: rerun `./install.sh aws` (terraform no-ops when unchanged, images
  re-push, migration reruns) then force new deployments — commands printed by
  `deploy/aws/build-and-push.sh`.
- Demo topology (public subnets + security groups, HTTP ALB) and the production
  hardening list: `deploy/aws/README.md`. **Teardown**: `terraform destroy` in
  `deploy/aws`.

---

## After any install

1. **Sign up** — the first account creates your organization; the setup checklist
   walks through the rest.
2. **Model connections** (Connections page): paste an Anthropic / OpenAI /
   Gemini / any OpenAI-compatible key, or point at an Ollama instance. Keys are
   sealed with AES-256-GCM at rest.
3. **Usage limits** (Projects page → "Daily usage limits"): daily token caps per
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

If that repository is not reachable from where you are installing — it is private, or you
are offline — the install still completes and the deployment falls back to the built-in
`Default` workflow. You will see a note saying so.

**The install never prompts for git credentials.** An unreachable pin fails fast rather
than asking for a GitHub username, so an unattended or scripted install cannot hang on a
prompt. If you do have access to a private pin, hand it over non-interactively:

```bash
export PYRRHULA_PLUGINS_TOKEN=<a token that can read the pinned repository>
./install.sh compose
```

### Adding packs without git

Neither of these needs git access, and both survive restarts.

**Upload.** Admin console → *Plugin repositories* → *Upload pack*: a `.zip` or `.tar.gz`
whose root holds `plugin.json`. An archive with a single wrapping directory (GitHub's
"Download ZIP", or `tar czf` of a checkout) is unwrapped for you. Works on every target
with no redeploy:

```bash
curl -sS -X POST http://localhost:8000/admin/plugin-repositories/upload \
  -H "Authorization: Bearer $PYRRHULA_ADMIN_TOKEN" \
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

Pyrrhula runs two models itself: one that embeds text for search, and one that reranks
what search found. The installers do not hardcode either — they ask the deployment what it
is configured to use and pre-warm that, so setting these *before* you install means the
right weights are fetched once instead of blocking your first knowledge call.

Set them in `.env` (compose) or `deploy/k8s/overlays/dev/secrets.env` (k8s):

```bash
# Examples, not recommendations -- any sentence-transformers model works.
PYRRHULA_EMBEDDING_MODEL=local/BAAI/bge-m3
PYRRHULA_EMBEDDING_DIMENSION=1024      # must match the model's output width
PYRRHULA_RERANKER_MODEL=local/BAAI/bge-reranker-v2-m3
PYRRHULA_RERANKER_ENABLED=true         # false = skip reranking entirely
```

Those particular models are what a default install fetches because they are multilingual,
permissively licensed (MIT and Apache-2.0) and run acceptably on CPU. They are a starting
point, not a recommendation: a smaller model is faster and cheaper to host, a
domain-specific one may retrieve better on your content, and a deployment that never
searches non-English text has no reason to pay for multilingual weights.

Two rules when changing them:

- **The dimension must match the model.** `PYRRHULA_EMBEDDING_DIMENSION` is the
  deployment's declared truth and is checked against the loaded model at startup, so a
  mismatch fails on boot rather than returning nothing at query time.
- **Existing vectors are not migrated.** Embeddings from a different model are not
  comparable; changing the embedding model on a deployment that already has knowledge
  orphans what is stored, and that content has to be re-indexed. Decide at install time
  if you can.

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
- **aws**: rebuild and push the image (`deploy/aws/build-and-push.sh`), then
  `terraform apply`; the migrate task runs on deploy.

Downgrading is not supported: some migrations carry data transformations whose
`downgrade()` exists for development only. Take a backup before upgrading
(`deploy/k8s/README.md` § Backups) — restoring the dump *is* the rollback.

## The one thing to never lose

Each install generates a `PYRRHULA_ENCRYPTION_KEY` that seals every stored
credential (model keys, repo tokens). Losing it means re-entering them all.

| target | where it lives | backup |
|---|---|---|
| compose | `.env` | `~/.config/pyrrhula/compose.env.bak` |
| k8s | `deploy/k8s/overlays/dev/secrets.env` | make one (the installer prints a reminder) |
| aws | Secrets Manager `pyrrhula/encryption-key` | AWS-managed; don't delete the secret |

## Verifying an install

`docs/deploy-verification.md` is the standing runbook (three end-to-end scenarios:
an executive discussion, an RPG encounter, a software-delivery flow with real PRs
and CI). It runs against any target via env overrides — the k8s example is in
`deploy/k8s/README.md`.

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
  check inside the embedding-model load; the installer flips pods to offline mode
  once the cache is populated, and a *partial* cache (interrupted first download)
  fails loudly on restart — delete the cache PVC contents and let it re-download.
- **k8s: engine declarations from outside the cluster** — use the host's LAN IP
  for `api_base`, not `host.containers.internal` (link-local, doesn't route).
- **aws: first page load 503s after install** — services pull the freshly pushed
  images on their next deployment cycle; give them a minute or force a new
  deployment.
- **Anything model-shaped hangs or errors** — check the connection's key and the
  usage limits page before debugging deeper; a hit cap pauses sessions by design.

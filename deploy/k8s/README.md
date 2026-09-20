# Pyrrhula on Kubernetes (k3s dev/test)

Two independent things live here:

1. **The exec engine on k8s** (`engine-rbac.yaml`) — keep the app wherever it runs
   (compose/podman) and let delegated coding agents execute as one-shot **Jobs** in a
   cluster namespace.
2. **The whole app on k8s** (`base/` + `overlays/dev/` + `dev-up.sh`) — api, worker,
   web, admin, postgres (pgvector), redis, migration Job, ingress.

Both were built against a single-node [k3s](https://k3s.io) on a dev machine; anything
conformant works with the caveats below.

## Local cluster (k3s) + dashboard

```bash
curl -sfL https://get.k3s.io | sh -          # systemd service; own containerd
sudo install -D -m 600 -o $USER -g $USER /etc/rancher/k3s/k3s.yaml ~/.kube/config
kubectl get nodes                             # Ready?
```

Dashboard (Headlamp; the official `kubernetes-dashboard` chart works the same way):

```bash
helm repo add headlamp https://kubernetes-sigs.github.io/headlamp/
helm install headlamp headlamp/headlamp -n kube-system
kubectl create serviceaccount headlamp-admin -n kube-system
kubectl create clusterrolebinding headlamp-admin --clusterrole=cluster-admin \
  --serviceaccount=kube-system:headlamp-admin
kubectl create token headlamp-admin -n kube-system --duration=8760h   # login token
kubectl -n kube-system port-forward svc/headlamp 8085:80              # http://localhost:8085
```

Terminal alternative: `k9s`.

## 1. Engine-only: delegated work as k8s Jobs

```bash
kubectl apply -f engine-rbac.yaml
kubectl -n pyrrhula-envs create token pyrrhula-runner --duration=8760h
```

Add to the worker's `PYRRHULA_EXEC_ENGINES` (see docs/exec-engines.md):

```json
{"key": "k3s", "kind": "kubernetes", "namespace": "pyrrhula-envs",
 "api_base": "https://host.containers.internal:6443", "token": "<runner token>",
 "verify_tls": false, "label": "Local Kubernetes",
 "git_http_base": "http://<host-LAN-IP>:8000"}
```

`git_http_base` is the per-engine route env pods use to clone/push the hosted repos —
they cannot resolve the podman container name, so point them at the api's published
host port. Tenants then pick the engine on the Repos page.

Use the host's **LAN IP** for `api_base`, not `host.containers.internal` — podman's
special hostname resolves to a link-local address that does not route to k3s's :6443
(observed live: ConnectTimeout).

## 2. The whole app

One command from the repo root:

```bash
./install.sh k8s --with-dashboard   # prereq checks + optional Headlamp, then dev-up
```

or directly:

```bash
./dev-up.sh     # build images -> import into k3s -> secrets (first run) -> apply -> migrate
```

Open **http://pyrrhula.localhost** (k3s traefik; `*.localhost` needs no DNS setup) and
sign up. The worker declares an in-cluster `kubernetes` engine automatically (its
ServiceAccount carries the runner Role), so delegations run as Jobs in
`pyrrhula-envs` out of the box. Rerun `dev-up.sh` after code changes.

Details worth knowing:

- **Secrets**: generated into `overlays/dev/secrets.env` (git-ignored) on first run.
  Back up the `ENCRYPTION_KEY` line — losing it orphans every sealed credential.
- **Shared blobs volume**: api and worker share the git store PVC. `ReadWriteOnce`
  works on a single node (both pods co-locate); multi-node clusters need an RWX
  storage class (NFS, Longhorn) — change `accessModes` in `base/api.yaml`.
- **Env isolation**: `base/envs-networkpolicy.yaml` limits env pods to DNS, the api's
  port 8000, and the internet (k3s enforces NetworkPolicy). Adjust the `except` CIDRs
  if your cluster uses non-default pod/service ranges.
- **Nothing host-specific in `base/`**: a clean install brings up postgres, redis, api,
  worker, web and admin — and nothing else. A host-local model provider and host-local
  MCP servers used to ship in the base with one developer's LAN address baked in; both
  are opt-in now.

- **Host ollama** (opt-in): add the component to `overlays/dev/kustomization.yaml`:

  ```yaml
  components:
    - ../../components/host-ollama
  ```

  then name a model in `overlays/dev/secrets.env` (keys there reach the app prefixed
  with `PYRRHULA_`): `ASSISTANT_MODEL=ollama/<tag>` and
  `ASSISTANT_API_BASE=http://ollama:11434`. Cloud-key deployments skip the component and
  set `ASSISTANT_MODEL` alone.

- **Agent web search**: a SearXNG instance ships in `base/searxng.yaml` and
  `PYRRHULA_WEB_SEARCH_URL` points at it, matching compose and the AWS stack — k8s
  previously had neither, so the feature silently did not exist. Not exposed outside the
  cluster; only personas with the web-search toggle reach it. Drop the file and clear the
  setting to disable, or point the setting at your own instance.

- **Host MCP servers** (opt-in): copy `host-mcp.example.yaml`, edit the name and port,
  `kubectl apply -f` it. An MCP server that already has a reachable URL needs no
  manifest at all — just grant it on a workspace. Full walkthrough: `docs/mcp.md`.

  Both of the above use a Service plus a hand-written EndpointSlice labelled
  `pyrrhula.io/host-endpoint: "true"`; `dev-up.sh` rewrites every such slice to the
  address of the machine it runs on, so no manifest carries a fixed IP.
- **Platform admin**: in the main UI — sign in with organization `admin`. `dev-up.sh`
  generates the account on first run and prints the email and password when it finishes;
  they live in `overlays/dev/secrets.env` as `ADMIN_EMAIL`/`ADMIN_PASSWORD`. Change the
  password in the app after first login — the bootstrap creates the account once and
  never updates it, so editing the file afterwards does not rotate anything. Existing
  deployments without these keys get them appended on the next run. The legacy token
  console remains reachable via
  `kubectl -n pyrrhula port-forward deploy/pyrrhula-admin 8100:8100` (deprecated).
- **Images, by default**: `dev-up.sh` builds them and imports them straight into k3s's
  containerd (`k3s ctr images import`), and `overlays/dev` uses those names
  (`localhost/pyrrhula:dev`) with pull policy `IfNotPresent`. A fresh machine needs
  nothing beyond k3s and podman/docker — no registry, no `registries.yaml`. The import
  needs sudo; `dev-up.sh` asks for it up front rather than dying mid-install.

  Do **not** set pull policy `Always` on these names: the image lives only inside
  containerd, so the kubelet would go looking for a registry host literally called
  `localhost` and land in `ImagePullBackOff`.

- **Images via a local registry** (opt-in: no per-update sudo, faster inner loop).
  Use `overlays/dev-registry`, which rewrites images to `localhost:5000/...` with pull
  policy `Always`. It needs host setup a fresh machine does not have:

  ```bash
  podman run -d --name pyr-registry --restart=always \
    -p 127.0.0.1:5000:5000 -v pyr-registry:/var/lib/registry docker.io/library/registry:2

  # containerd must trust the plain-HTTP registry (one-time)
  sudo mkdir -p /etc/rancher/k3s
  sudo tee /etc/rancher/k3s/registries.yaml >/dev/null <<'YAML'
  mirrors:
    "localhost:5000":
      endpoint:
        - "http://127.0.0.1:5000"
  YAML
  sudo systemctl restart k3s
  ```

  Then bring the stack up in registry mode — `dev-up.sh` pushes instead of importing
  and applies the registry overlay:

  ```bash
  PYRRHULA_K8S_REGISTRY=127.0.0.1:5000 deploy/k8s/dev-up.sh
  ```

  After that, an image update is just:
  `podman build -t pyrrhula:dev -f docker/Dockerfile . && podman push --tls-verify=false localhost/pyrrhula:dev 127.0.0.1:5000/pyrrhula:dev && kubectl -n pyrrhula rollout restart deploy/pyrrhula-api deploy/pyrrhula-worker`.
- **Verification**: the standing runbook (docs/deploy-verification.md) runs against
  this stack via env overrides, e.g.:

  ```bash
  PYRRHULA_VERIFY_API=http://pyrrhula.localhost/api \
  PYRRHULA_VERIFY_PG_EXEC="kubectl -n pyrrhula exec statefulset/postgres --" \
  PYRRHULA_VERIFY_API_EXEC="kubectl -n pyrrhula exec deploy/pyrrhula-api --" \
  python scripts/verify_deploy.py exec rpg swe
  ```

## Other clusters (not k3s)

`dev-up.sh` and `overlays/dev` exist for one shape of cluster: single-node k3s on the
machine you are sitting at. Everything in `base/` is portable; four things in that
default path are not, and three of them fail *silently* elsewhere.

Start from the template and change the marked values:

```bash
cp -r deploy/k8s/overlays/cluster deploy/k8s/overlays/mycluster
$EDITOR deploy/k8s/overlays/mycluster/kustomization.yaml
kubectl apply -k deploy/k8s/overlays/mycluster
```

**1. Images.** The base names `localhost/pyrrhula:dev`, which exists only in a local
containerd. Build and push to a registry your nodes can pull from; the overlay's `images:`
block rewrites every reference, initContainers included. `dev-up.sh` is not part of this
flow — it builds, imports and applies `overlays/dev`. Build in CI and apply the overlay.

**2. Ingress class.** The base Ingress names no `ingressClassName`, which works on k3s
only because traefik is installed as the *default* IngressClass. Where none is marked
default — common on EKS, GKE and any hand-installed nginx — no controller claims the
Ingress and nothing serves it, with no error to explain the silence. The overlay sets it,
and `installers/k8s.sh` now warns when the cluster has no default.

**3. Storage class.** The claims name none, so they bind through the cluster default.
Managed clusters have one; bare-metal frequently has none and the claims pend forever.
The overlay names it, and the installer warns.

**4. One node's worth of capacity.** `blobs` and `hf-cache` are `ReadWriteOnce` and are
mounted by **both** the api and the worker. A `podAffinity` in `base/worker.yaml` keeps
the two on the same node, because ReadWriteOnce means exactly one node may mount them —
without it the scheduler is free to split them and whichever pod starts second hangs in
`ContainerCreating` with a Multi-Attach error.

That pins the stack to a single node. To spread it, move those two claims to a
**ReadWriteMany** class — EFS, Filestore, NFS, Longhorn — change their `accessModes`, and
delete the `affinity` block from `base/worker.yaml`. Nothing else depends on the
co-location.

**Secrets** are not generated by the template on purpose: `overlays/dev` writes a
`secrets.env` next to the manifests, which is right for a laptop and wrong for a cluster.
Use a sealed secret, an external-secrets operator, or create it out of band —

```bash
kubectl -n pyrrhula create secret generic pyrrhula-secrets --from-env-file=./secrets.env
```

— with the same keys `overlays/dev/secrets.env` lists. The **migration Job** (`pyrrhula-migrate`)
still has to run before the api starts; `kubectl apply -k` creates it, and a redeploy needs
it deleted first because a completed Job is immutable:

```bash
kubectl -n pyrrhula delete job pyrrhula-migrate --ignore-not-found
kubectl apply -k deploy/k8s/overlays/mycluster
```

**Retrieval models.** `dev-up.sh` pre-warms the embedding cache and then flips the pods to
`HF_HUB_OFFLINE=1`. Applying an overlay directly does neither, so the first knowledge call
downloads ~2.2GB into the `hf-cache` claim. Either let it, or fetch from *Admin → Retrieval
models* (upload a cache tarball on a cluster with no route to huggingface.co), then set the
offline vars yourself — an unauthenticated hub check has no timeout and can wedge the api's
event loop.

## Workflow packs when the plugin repository is unreachable

The install never asks for git credentials. If the pinned repository in
`deploy/plugins.json` is private or unreachable, the build says so and continues with the
built-in workflows -- the platform runs, it just has fewer workflows. Add the rest either
way below; neither needs git.

**Upload (no manifest change).** Admin console -> *Plugin repositories* -> *Upload pack*,
with a `.zip` or `.tar.gz` whose root holds `plugin.json` (a single wrapping directory,
as produced by GitHub's "Download ZIP", is unwrapped for you). The content lands in the
blobs PVC, so it survives restarts and is shared by the api and worker. Or from a shell:

```bash
curl -sS -X POST http://<host>/admin/plugin-repositories/upload \
  -H "Authorization: Bearer $PYRRHULA_ADMIN_TOKEN" \
  -F name=my-workflows -F file=@my-workflows.zip
```

**Drop directory (mounted).** Every subdirectory holding a `plugin.json` under
`PYRRHULA_PLUGIN_DROP_DIR` (default `/app/plugins-local`) is registered at boot. Nothing
is mounted there by default in k8s; supply one and patch it into **both** the api and the
worker -- the api syncs it at boot, the worker reads pack content for running sessions:

```bash
kubectl -n pyrrhula create configmap my-workflows \
  --from-file=plugin.json=./my-workflows/plugin.json \
  --from-file=./my-workflows/wf-key/
```

then mount it at `/app/plugins-local/my-workflows`. A ConfigMap is capped at 1 MiB and
flattens directories -- for anything larger or deeper, use a small RWX PVC, or just
upload. Packs here are read-only to the platform: remove one by deleting it from the
directory and restarting, not from the console.

## Backups and the restore drill

`base/backup-cronjob.yaml` dumps the whole database nightly (03:17 UTC, custom format)
to the `pyrrhula-backups` PVC and prunes past `KEEP_DAYS` (14). A backup nobody has
restored is a hope, not a backup — run the drill after any schema-shaped change:

```bash
# take one now
kubectl -n pyrrhula create job backup-now --from=cronjob/pyrrhula-backup
kubectl -n pyrrhula logs job/backup-now

# restore the newest dump into a scratch database and count what came back
kubectl -n pyrrhula exec -i statefulset/postgres -- \
  psql -U pyrrhula -d postgres -tAc "CREATE DATABASE restore_drill"
# (run a pod that mounts the backups PVC; see the drill Job in this repo's history)
kubectl -n pyrrhula exec -i statefulset/postgres -- \
  psql -U pyrrhula -d restore_drill -tAc "select count(*) from message"
```

Verified 2026-09-09 (after the preview/publication/steward/activation/deployment-setting
migrations): 650KB dump restored with 10 tenants / 33 messages / 46 secrets, every
new-schema table present, RLS policies restored, and the audit hash chain recomputed
intact across all 9 non-library tenants.

## Audit chain verification

`GET /api/admin/tenants/{id}/audit/verify` recomputes the hash chain and returns any
broken row ids. Tamper-evident storage only pays off if something actually checks it —
cron this per tenant, or watch it from the admin console.

## TLS

`deploy/k8s/tls/` is opt-in (install [cert-manager](https://cert-manager.io) first):

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
kubectl -n cert-manager rollout status deploy/cert-manager

# local/dev: real TLS, self-signed CA (the browser warns once)
kubectl apply -f deploy/k8s/tls/issuers.yaml -f deploy/k8s/tls/dev-localhost-tls.yaml
curl -k https://pyrrhula.localhost/api/health

# production: edit the email in issuers.yaml and the host in ingress-tls-patch.yaml
kubectl apply -k deploy/k8s/tls
```

Three ClusterIssuers: `pyrrhula-selfsigned` (dev/air-gapped),
`pyrrhula-letsencrypt-staging` (rehearse issuance without burning rate limits),
`pyrrhula-letsencrypt-prod`. HTTP-01 solvers on purpose — no cloud credentials need to
live in a self-hosted cluster.

**Set `PYRRHULA_COOKIE_SECURE=true` wherever TLS terminates.** That is the other half
of the job: it makes the browser refuse to send the session cookie over plain http.
Verified 2026-09-04 on this cluster: cert-manager issued the certificate, and both
`https://pyrrhula.localhost/` and `/api/health` served 200.

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
- **Host ollama**: the `ollama` Service points at the host's IP (patched by
  `dev-up.sh`), so local models keep working from inside the cluster. Cloud-key-only
  deployments can delete `base/ollama-host.yaml`.
- **Platform admin**: in the main UI — sign in with organization `admin` (bootstrap
  the account with `PYRRHULA_ADMIN_EMAIL`/`PYRRHULA_ADMIN_PASSWORD` in `secrets.env`).
  The legacy token console remains reachable via
  `kubectl -n pyrrhula port-forward deploy/pyrrhula-admin 8100:8100` (deprecated).
- **Images via local registry** (no `k3s ctr images import`, no per-update sudo):
  a rootless registry container serves `127.0.0.1:5000`
  (`podman run -d --name pyr-registry --restart=always -p 127.0.0.1:5000:5000 -v pyr-registry:/var/lib/registry docker.io/library/registry:2`);
  the dev overlay rewrites images to `localhost:5000/...` with pull policy Always.
  ONE-TIME setup — containerd must trust the plain-HTTP registry:

  ```bash
  sudo mkdir -p /etc/rancher/k3s
  sudo tee /etc/rancher/k3s/registries.yaml >/dev/null <<'YAML'
  mirrors:
    "localhost:5000":
      endpoint:
        - "http://127.0.0.1:5000"
  YAML
  sudo systemctl restart k3s
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

#!/usr/bin/env bash
# Bring the whole stack up on a local k3s: build images, import them into k3s's
# containerd, generate dev secrets on first run, apply the dev overlay, run the
# migration Job. Idempotent -- rerun after code changes to redeploy.
set -euo pipefail
cd "$(dirname "$0")"
REPO_ROOT="$(cd ../.. && pwd)"
ENGINE=$(command -v podman || command -v docker)

# The k3s image import below needs sudo. Fail HERE, loudly, when sudo cannot prompt
# (no TTY -- e.g. launched from an IDE task runner) instead of dying mid-install
# after minutes of building. Registry mode pushes instead of importing, so it needs
# no sudo at all.
if [ -z "${PYRRHULA_K8S_REGISTRY:-}" ] && command -v k3s >/dev/null && ! sudo -n true 2>/dev/null; then
  if [ ! -t 0 ]; then
    echo "ERROR: this install needs sudo (k3s image import), but there is no terminal"
    echo "       to type the password into. Run it from an interactive shell, or"
    echo "       pre-authorize in any terminal first:  sudo -v"
    exit 1
  fi
  echo "== sudo is needed for the k3s image import -- authorizing now"
  sudo -v
fi

echo "== fetching workflow plugins"
python3 "$REPO_ROOT/scripts/fetch_plugins.py"

echo "== build images"
"$ENGINE" build -t localhost/pyrrhula:dev -f "$REPO_ROOT/docker/Dockerfile" "$REPO_ROOT"
"$ENGINE" build -t localhost/pyrrhula-web:dev -f "$REPO_ROOT/docker/web.Dockerfile" "$REPO_ROOT"

# Two ways to get the images to the kubelet. The default imports them straight into
# k3s's containerd, because that works on a machine with nothing set up beyond k3s and
# a container engine. Setting PYRRHULA_K8S_REGISTRY switches to pushing at a registry
# you already run (no sudo per update) -- see overlays/dev-registry for its prerequisites.
OVERLAY=overlays/dev
if [ -n "${PYRRHULA_K8S_REGISTRY:-}" ]; then
  echo "== push images to $PYRRHULA_K8S_REGISTRY"
  for img in pyrrhula pyrrhula-web; do
    "$ENGINE" push --tls-verify=false "localhost/$img:dev" "$PYRRHULA_K8S_REGISTRY/$img:dev"
  done
  OVERLAY=overlays/dev-registry
else
  echo "== import into k3s containerd (needs sudo)"
  "$ENGINE" save localhost/pyrrhula:dev | sudo k3s ctr images import -
  "$ENGINE" save localhost/pyrrhula-web:dev | sudo k3s ctr images import -
fi

SECRETS=overlays/dev/secrets.env
if [ ! -f "$SECRETS" ]; then
  echo "== generating $SECRETS (first run)"
  PG=$(openssl rand -hex 24)
  APPPW=$(openssl rand -hex 24)
  cat > "$SECRETS" <<EOF
POSTGRES_PASSWORD=$PG
APP_DB_PASSWORD=$APPPW
JWT_SECRET=$(openssl rand -base64 48 | tr -d '\n')
ENCRYPTION_KEY=$(openssl rand -base64 32)
ADMIN_EMAIL=admin@example.com
ADMIN_PASSWORD=$(openssl rand -hex 12)
DATABASE_URL=postgresql+asyncpg://pyrrhula:$PG@postgres:5432/pyrrhula
APP_DATABASE_URL=postgresql+asyncpg://pyrrhula_app:$APPPW@postgres:5432/pyrrhula
EOF
  chmod 600 "$SECRETS"
  echo "   BACK UP the ENCRYPTION_KEY line -- losing it orphans every sealed credential."
fi

# Top-up for deployments created before the installer generated an admin login. Without
# these there is no way to sign in as the platform admin at all.
if ! grep -q '^ADMIN_EMAIL=' "$SECRETS"; then
  echo "== adding a platform-admin login to $SECRETS"
  {
    echo "ADMIN_EMAIL=admin@example.com"
    echo "ADMIN_PASSWORD=$(openssl rand -hex 12)"
  } >> "$SECRETS"
fi

# Anything running on the host that pods reach by name -- the opt-in ollama component,
# an MCP server attached from host-mcp.example.yaml -- is wired with a hand-written
# EndpointSlice whose address is a placeholder. Fill in THIS machine's address.
HOST_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}' || true)

echo "== apply"
kubectl delete job pyrrhula-migrate -n pyrrhula --ignore-not-found
kubectl apply -k "$OVERLAY"
# Tenancy, set on the deployments rather than in base's ConfigMap so a rerun with the
# other value actually switches an existing cluster. base's literal stays the default
# for anyone applying the manifests without this script.
kubectl -n pyrrhula set env deploy/pyrrhula-api deploy/pyrrhula-worker \
  "PYRRHULA_SINGLE_TENANT_UI=${PYRRHULA_SINGLE_TENANT_UI:-true}" >/dev/null
if [ -n "$HOST_IP" ]; then
  # Discovered by label, never by a hardcoded list: attaching a host MCP server should
  # be copying one manifest, not editing the installer. The old list named one
  # developer's sidecars and shipped them to everyone.
  SLICES=$(kubectl -n pyrrhula get endpointslice -l pyrrhula.io/host-endpoint=true \
    -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)
  for slice in $SLICES; do
    echo "   host endpoint $slice -> $HOST_IP"
    kubectl -n pyrrhula patch endpointslice "$slice" --type=json \
      -p "[{\"op\":\"replace\",\"path\":\"/endpoints/0/addresses/0\",\"value\":\"$HOST_IP\"}]" \
      >/dev/null || true
  done
fi

# Make a re-run actually redeploy the code that was just built.
#
# `kubectl apply` changes nothing when the Deployment spec is identical, and the `dev`
# tag is mutable -- so importing a rebuilt image under the same tag leaves every EXISTING
# pod running the old code, silently. This script promises the opposite at the top of the
# file ("rerun after code changes to redeploy"), and it was not true: a fix could be
# built, imported and applied, and the running pods would never pick it up.
#
# Stamping the built image's ID onto the pod template is precise rather than blunt: an
# unchanged ID patches to a no-op and nothing restarts, a new ID rolls the deployment.
stamp_image() { # stamp_image <deployment> <local image ref>
  _id=$("$ENGINE" image inspect --format '{{.Id}}' "$2" 2>/dev/null || echo unknown)
  _patch='{"spec":{"template":{"metadata":{"annotations":{"pyrrhula.io/image-id":"'"$_id"'"}}}}}'
  kubectl -n pyrrhula patch deploy "$1" -p "$_patch" >/dev/null
}
echo "== roll deployments whose image changed"
for _d in pyrrhula-api pyrrhula-worker; do
  stamp_image "$_d" localhost/pyrrhula:dev
done
stamp_image pyrrhula-web localhost/pyrrhula-web:dev

# Wait, but say WHY when nothing moves. A bare `kubectl wait` prints nothing for five
# minutes and then "timed out" -- the least useful thing an installer can do, because
# the real cause (an image the kubelet cannot pull, a claim no provisioner will bind)
# is sitting in the events the whole time. Report it while still waiting, then fail
# with it rather than making the reader go find it.
diagnose() {
  echo "   -- pods"
  kubectl -n pyrrhula get pods 2>&1 | sed 's/^/      /'
  unbound=$(kubectl -n pyrrhula get pvc \
    -o jsonpath='{range .items[?(@.status.phase!="Bound")]}{.metadata.name} {end}' 2>/dev/null || true)
  if [ -n "${unbound// /}" ]; then
    echo "   -- volume claims not bound: $unbound"
    echo "      Nothing has provisioned storage. On k3s that is local-path:"
    echo "        kubectl -n kube-system get pods -l app=local-path-provisioner"
    echo "        kubectl -n kube-system logs -l app=local-path-provisioner --tail=20"
    echo "      If it is running but idle, restart it:"
    echo "        kubectl -n kube-system rollout restart deploy/local-path-provisioner"
  fi
  if kubectl -n pyrrhula get pods -o jsonpath='{range .items[*]}{.status.containerStatuses[*].state.waiting.reason}{"\n"}{end}' 2>/dev/null \
     | grep -qE 'ImagePullBackOff|ErrImagePull'; then
    echo "   -- an image could not be pulled"
    if [ -n "${PYRRHULA_K8S_REGISTRY:-}" ]; then
      echo "      Registry mode is on (PYRRHULA_K8S_REGISTRY=$PYRRHULA_K8S_REGISTRY)."
      echo "      Check the registry is up and that containerd trusts it"
      echo "      (/etc/rancher/k3s/registries.yaml -- see overlays/dev-registry)."
    else
      echo "      The images are imported into containerd, so this usually means the"
      echo "      import did not happen or went to a different runtime. Verify with:"
      echo "        sudo k3s ctr images ls | grep pyrrhula"
    fi
  fi
  echo "   -- recent warnings"
  kubectl -n pyrrhula get events --field-selector type=Warning \
    --sort-by=.lastTimestamp 2>/dev/null | tail -6 | sed 's/^/      /'
}

# $1 = human label, $2 = shell snippet that succeeds once the thing is ready
wait_with_reason() {
  label="$1"; check="$2"
  echo "== waiting for $label"
  deadline=$(( $(date +%s) + 300 )); warned=0
  while ! eval "$check" >/dev/null 2>&1; do
    now=$(date +%s)
    if [ "$now" -ge "$deadline" ]; then
      echo "ERROR: $label did not become ready within 5 minutes."
      diagnose
      exit 1
    fi
    if [ "$warned" -eq 0 ] && [ "$now" -ge "$(( deadline - 255 ))" ]; then
      warned=1
      echo "   still waiting after 45s -- what the cluster says right now:"
      diagnose
    fi
    sleep 5
  done
}

wait_with_reason "the migration" \
  '[ "$(kubectl -n pyrrhula get job pyrrhula-migrate -o jsonpath="{.status.succeeded}")" = "1" ]'
# readyReplicas is already satisfied by the OLD pod while a rollout is in flight, so
# ask whether the rollout itself finished -- otherwise the installer declares success
# on the very code it just replaced.
wait_with_reason "the api" \
  'kubectl -n pyrrhula rollout status deploy/pyrrhula-api --timeout=10s'

# The retrieval models are NOT downloaded here -- see deploy/installers/compose.sh for
# why: which model a deployment runs is its operator's decision, made in Admin ->
# Models, not multiple gigabytes spent before they have seen a screen. The pods run
# HF_HUB_OFFLINE=1 (base kustomization), so the runtime never reaches the network on its
# own; an explicit admin fetch lifts that for the duration of the fetch alone.

# --- does this deployment actually work? ------------------------------------------
# Mandatory. The rollout status above proves pods are Ready, which is not the same as
# working: a worker that OOMs on its first job is Ready right up until it claims one.
# This drives a document through the whole loop instead. Pack-free and model-free.
echo "== verifying the deployment"
if ! kubectl -n pyrrhula exec deploy/pyrrhula-api -- python /app/deploy-smoke.py; then
  echo
  echo "ERROR: the stack is up but cannot do real work. Details above."
  kubectl -n pyrrhula get pods
  exit 1
fi

# POSIX sed, not `grep -oP`: -P is a GNU extension and BSD grep (macOS) has no such
# flag, so the credentials would print as nothing on exactly the fresh machine that
# needs them most.
ADMIN_EMAIL_VALUE=$(sed -n 's/^ADMIN_EMAIL=//p' "$SECRETS" 2>/dev/null || true)
ADMIN_PASSWORD_VALUE=$(sed -n 's/^ADMIN_PASSWORD=//p' "$SECRETS" 2>/dev/null || true)

echo
echo "Up. Open http://pyrrhula.localhost"
echo
echo "  Sign in as the platform admin:"
echo "    organization  admin"
echo "    email         $ADMIN_EMAIL_VALUE"
echo "    password      $ADMIN_PASSWORD_VALUE"
echo
echo "  Or Register to create your own organization."
echo
echo "  Generated on first run and stored in $SECRETS."
echo "  Change the password IN THE APP after first login -- the account is created once"
echo "  and editing the file afterwards does not rotate it."

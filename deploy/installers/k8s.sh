#!/usr/bin/env bash
# Kubernetes installer: prereq checks, optional dashboard, then the dev-up flow
# (build images -> import into k3s -> secrets -> apply -> migrate). Built and
# verified against single-node k3s; any conformant cluster works with a reachable
# image registry -- see deploy/k8s/README.md, "Other clusters", and the copyable
# overlay at deploy/k8s/overlays/cluster.
#
#   deploy/installers/k8s.sh [--check] [--with-dashboard]
set -euo pipefail
cd "$(dirname "$0")/../.."

say()  { printf '\033[1m== %s\033[0m\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

WITH_DASHBOARD=0
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --with-dashboard) WITH_DASHBOARD=1 ;;
    *) fail "unknown flag $arg" ;;
  esac
done

# --- prerequisites ----------------------------------------------------------------
command -v kubectl >/dev/null || fail "need kubectl (with k3s: sudo install -D -m600 -o \$USER /etc/rancher/k3s/k3s.yaml ~/.kube/config)"
# k3s's bundled kubectl defaults to the root-only /etc/rancher path; point it at the
# user copy when the caller hasn't.
[ -z "${KUBECONFIG:-}" ] && [ -r "$HOME/.kube/config" ] && export KUBECONFIG="$HOME/.kube/config"
command -v openssl >/dev/null || fail "need openssl"
ENGINE=$(command -v podman || command -v docker) || fail "need podman or docker to build images"
kubectl get nodes >/dev/null 2>&1 || fail "kubectl cannot reach a cluster (KUBECONFIG=~/.kube/config exported?)"
say "cluster: $(kubectl config current-context 2>/dev/null || echo '?') ($(kubectl get nodes --no-headers | wc -l) node(s))"

K3S=""
command -v k3s >/dev/null && K3S=1
if [ -z "$K3S" ]; then
  echo "   note: no local k3s binary -- image import is k3s-specific. On other clusters,"
  echo "   push the images to a registry your nodes can pull from and apply the cluster"
  echo "   overlay instead (deploy/k8s/README.md, 'Other clusters')."
fi

# Two cluster defaults the base manifests rely on without naming. k3s provides both, so
# neither was ever checked -- and on a cluster missing them the failure is silent in the
# way that wastes the most time: the Ingress is simply never claimed, or the claims never
# bind, with no error that says why. Warn rather than fail: a cluster can be fine with
# neither if the operator is applying their own overlay.
if ! kubectl get ingressclass -o jsonpath='{.items[*].metadata.annotations.ingressclass\.kubernetes\.io/is-default-class}' 2>/dev/null | grep -q true; then
  echo "   WARNING: no IngressClass is marked default in this cluster."
  echo "   The base Ingress names no class, so nothing will claim it and the UI will not"
  echo "   be reachable -- with no error anywhere. Set spec.ingressClassName (see"
  echo "   deploy/k8s/overlays/cluster) or mark one of these default:"
  kubectl get ingressclass --no-headers 2>/dev/null | sed 's/^/     /' || echo "     (none installed at all)"
fi
if ! kubectl get storageclass -o jsonpath='{.items[*].metadata.annotations.storageclass\.kubernetes\.io/is-default-class}' 2>/dev/null | grep -q true; then
  echo "   WARNING: no StorageClass is marked default in this cluster."
  echo "   The volume claims name none, so they will stay Pending forever. Set"
  echo "   spec.storageClassName (see deploy/k8s/overlays/cluster) or mark one default."
fi

# ReadWriteOnce claims shared by the api and the worker are what the base's podAffinity
# exists for. It keeps them on one node, which is correct but caps the stack at a single
# node's worth of capacity -- worth saying out loud before someone wonders why a 5-node
# cluster runs everything in one place.
NODES=$(kubectl get nodes --no-headers 2>/dev/null | wc -l | tr -d ' ')
if [ "${NODES:-1}" -gt 1 ]; then
  echo "   note: $NODES nodes. The api and worker share ReadWriteOnce volumes, so a"
  echo "   podAffinity pins them to one node together. For them to spread, switch"
  echo "   blobs/hf-cache to a ReadWriteMany class and drop that affinity"
  echo "   (deploy/k8s/README.md, 'Other clusters')."
fi

if [ "$CHECK_ONLY" = 1 ]; then say "prerequisites OK"; exit 0; fi

# --- optional dashboard (Headlamp) ------------------------------------------------
if [ "$WITH_DASHBOARD" = 1 ]; then
  command -v helm >/dev/null || fail "--with-dashboard needs helm"
  say "installing Headlamp dashboard"
  helm repo add headlamp https://kubernetes-sigs.github.io/headlamp/ >/dev/null 2>&1 || true
  helm upgrade --install headlamp headlamp/headlamp -n kube-system >/dev/null
  kubectl -n kube-system get clusterrolebinding headlamp-admin >/dev/null 2>&1 || \
    kubectl create clusterrolebinding headlamp-admin --clusterrole=cluster-admin \
      --serviceaccount=kube-system:headlamp >/dev/null
  mkdir -p ~/.config/pyrrhula
  kubectl create token headlamp -n kube-system --duration=8760h > ~/.config/pyrrhula/headlamp-token.txt
  chmod 600 ~/.config/pyrrhula/headlamp-token.txt
  echo "   dashboard: kubectl -n kube-system port-forward svc/headlamp 8085:80"
  echo "   login token saved to ~/.config/pyrrhula/headlamp-token.txt"
fi

# --- the stack --------------------------------------------------------------------
exec deploy/k8s/dev-up.sh

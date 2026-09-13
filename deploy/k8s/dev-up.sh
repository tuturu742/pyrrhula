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
# after minutes of building.
if command -v k3s >/dev/null && ! sudo -n true 2>/dev/null; then
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

echo "== import into k3s containerd (needs sudo)"
"$ENGINE" save localhost/pyrrhula:dev | sudo k3s ctr images import -
"$ENGINE" save localhost/pyrrhula-web:dev | sudo k3s ctr images import -

SECRETS=overlays/dev/secrets.env
if [ ! -f "$SECRETS" ]; then
  echo "== generating $SECRETS (first run)"
  PG=$(openssl rand -hex 24)
  APPPW=$(openssl rand -hex 24)
  cat > "$SECRETS" <<EOF
POSTGRES_PASSWORD=$PG
APP_DB_PASSWORD=$APPPW
JWT_SECRET=$(openssl rand -base64 48 | tr -d '\n')
ADMIN_TOKEN=$(openssl rand -hex 24)
ENCRYPTION_KEY=$(openssl rand -base64 32)
DATABASE_URL=postgresql+asyncpg://pyrrhula:$PG@postgres:5432/pyrrhula
APP_DATABASE_URL=postgresql+asyncpg://pyrrhula_app:$APPPW@postgres:5432/pyrrhula
EOF
  chmod 600 "$SECRETS"
  echo "   BACK UP the ENCRYPTION_KEY line -- losing it orphans every sealed credential."
fi

# Point the in-cluster ollama Service at THIS machine (host GPU); skip if offline.
HOST_IP=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}' || true)

echo "== apply"
kubectl delete job pyrrhula-migrate -n pyrrhula --ignore-not-found
kubectl apply -k overlays/dev
if [ -n "$HOST_IP" ]; then
  for slice in ollama-1 godot-mcp-1 comfy-mcp-1; do
    kubectl -n pyrrhula patch endpointslice "$slice" --type=json \
      -p "[{\"op\":\"replace\",\"path\":\"/endpoints/0/addresses/0\",\"value\":\"$HOST_IP\"}]" \
      || true
  done
fi

echo "== waiting for migration"
kubectl -n pyrrhula wait --for=condition=complete job/pyrrhula-migrate --timeout=300s
kubectl -n pyrrhula rollout status deploy/pyrrhula-api --timeout=300s

# Pre-warm the embedding model (bge-m3, ~2.2GB) during install: a cold in-request
# download blocks the first knowledge/assistant call for minutes and has been seen
# wedging the api's event loop on an HF-hub rate-limit stall.
HF_SIZE=$(kubectl -n pyrrhula exec deploy/pyrrhula-api -- du -sm /app/.cache/huggingface 2>/dev/null | cut -f1 || echo 0)
if [ "${HF_SIZE:-0}" -le 1000 ]; then
  echo "== downloading the retrieval models (one-time, please wait)"
  # No model name here: the deployment's choice lives in its configuration
  # (PYRRHULA_EMBEDDING_MODEL / PYRRHULA_RERANKER_MODEL, see docs/install.md), so ask
  # the app what it is set to and warm that. A hardcoded name would pre-warm the wrong
  # model for anyone who changed it, and charge them the cold download anyway.
  WARM='
from core.config import get_settings
from sentence_transformers import CrossEncoder, SentenceTransformer
s = get_settings()
SentenceTransformer(s.embedding_model.split("/", 1)[-1])
if s.reranker_enabled:
    CrossEncoder(s.reranker_model.split("/", 1)[-1])
'
  # Two attempts with a pause: right after the stack comes up, in-cluster DNS can
  # still be settling (observed live: the first attempt failed on name resolution
  # seconds after rollout; the retry succeeded).
  for attempt in 1 2; do
    if kubectl -n pyrrhula exec deploy/pyrrhula-api -- python -c "$WARM" >/dev/null 2>&1
    then break; fi
    if [ "$attempt" = 1 ]; then
      echo "   first attempt failed (startup DNS can lag) -- retrying in 20s"
      sleep 20
    else
      echo "   warning: pre-download failed; the first knowledge call will fetch it"
    fi
  done
  HF_SIZE=$(kubectl -n pyrrhula exec deploy/pyrrhula-api -- du -sm /app/.cache/huggingface 2>/dev/null | cut -f1 || echo 0)
fi

# Once the cache is populated, run offline: an unauthenticated HF-hub check can HANG
# (rate-limit stall, no timeout) inside the in-process model load and wedge the api's
# event loop (observed live: NotReady for 13+ min at idle CPU).
if [ "${HF_SIZE:-0}" -gt 1000 ]; then
  kubectl -n pyrrhula set env deploy/pyrrhula-api deploy/pyrrhula-worker \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
  echo "   embedding cache present (${HF_SIZE}MB) -> pods set to HF offline mode"
fi

echo
echo "Up. Open http://pyrrhula.localhost and Sign up."
echo "Admin console: kubectl -n pyrrhula port-forward deploy/pyrrhula-admin 8100:8100"
echo "Admin token:   grep ADMIN_TOKEN $SECRETS"

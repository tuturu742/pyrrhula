#!/usr/bin/env bash
# Compose installer: docker or podman, one machine, smallest footprint.
# Idempotent -- rerun after `git pull` to upgrade. `--check` verifies prereqs only.
set -euo pipefail
cd "$(dirname "$0")/../.."

say()  { printf '\033[1m== %s\033[0m\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- prerequisites ----------------------------------------------------------------
ENGINE=""
COMPOSE=()
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  ENGINE=docker; COMPOSE=(docker compose)
elif command -v podman >/dev/null; then
  ENGINE=podman
  if podman compose version >/dev/null 2>&1; then COMPOSE=(podman compose)
  elif command -v podman-compose >/dev/null; then COMPOSE=(podman-compose)
  else fail "podman found but no compose provider (install podman-compose or the compose plugin)"
  fi
else
  fail "need docker (with the compose plugin) or podman"
fi
command -v openssl >/dev/null || fail "need openssl (secret generation)"
say "engine: $ENGINE (${COMPOSE[*]})"

# Exec environments (agents building/testing code) need the engine's API socket.
SOCKET=""
if [ "$ENGINE" = podman ]; then
  CANDIDATE="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
  if [ -S "$CANDIDATE" ]; then SOCKET="$CANDIDATE"
  else
    echo "   note: no podman socket at $CANDIDATE -- delegated coding agents will be"
    echo "   disabled until you run: systemctl --user enable --now podman.socket"
  fi
else
  [ -S /var/run/docker.sock ] && SOCKET=/var/run/docker.sock
fi

if [ "${1:-}" = "--check" ]; then
  say "prerequisites OK"; exit 0
fi

# Explicit project name: without it, compose derives the project from the compose
# FILE's directory ("docker"), which collides across checkouts -- observed live as a
# fresh install silently mounting a stale postgres volume whose passwords no longer
# matched the fresh .env.
PROJECT="${PYRRHULA_COMPOSE_PROJECT:-pyrrhula}"
CARGS=(-p "$PROJECT" -f docker/compose.selfhost.yml)

# --- secrets ----------------------------------------------------------------------
FRESH_ENV=0
if [ ! -f .env ]; then
  FRESH_ENV=1
  say "generating .env (secrets)"
  cat > .env <<EOF
PYRRHULA_POSTGRES_PASSWORD=$(openssl rand -hex 24)
PYRRHULA_APP_DB_PASSWORD=$(openssl rand -hex 24)
PYRRHULA_JWT_SECRET=$(openssl rand -base64 48 | tr -d '\n')
PYRRHULA_ADMIN_TOKEN=$(openssl rand -hex 24)
PYRRHULA_ENCRYPTION_KEY=$(openssl rand -base64 32)
EOF
  [ -n "$SOCKET" ] && echo "PYRRHULA_ENGINE_SOCKET=$SOCKET" >> .env
  chmod 600 .env
  mkdir -p ~/.config/pyrrhula
  cp .env ~/.config/pyrrhula/compose.env.bak && chmod 600 ~/.config/pyrrhula/compose.env.bak
  echo "   secrets written to .env (backup: ~/.config/pyrrhula/compose.env.bak)"
  echo "   BACK UP the PYRRHULA_ENCRYPTION_KEY line -- losing it orphans every stored credential."
else
  say "using existing .env"
fi

# Fresh secrets over an existing database can never work (postgres only applies its
# password on FIRST init) -- refuse instead of producing a half-broken stack.
if [ "$FRESH_ENV" = 1 ] && podman volume exists "${PROJECT}_pyrrhula-postgres" 2>/dev/null; then
  fail "a previous install's data exists (volume ${PROJECT}_pyrrhula-postgres) but .env was just
       generated with NEW secrets. Either restore the previous .env (backup:
       ~/.config/pyrrhula/compose.env.bak) or wipe the old install first:
       ${COMPOSE[*]} -p $PROJECT -f docker/compose.selfhost.yml down -v"
fi
if [ "$FRESH_ENV" = 1 ] && command -v docker >/dev/null 2>&1 && [ "$ENGINE" = docker ] \
   && docker volume inspect "${PROJECT}_pyrrhula-postgres" >/dev/null 2>&1; then
  fail "a previous install's data exists (volume ${PROJECT}_pyrrhula-postgres); restore the old
       .env or run: ${COMPOSE[*]} -p $PROJECT -f docker/compose.selfhost.yml down -v"
fi

# --- up ---------------------------------------------------------------------------
say "fetching workflow plugins (deploy/plugins.json)"
python3 scripts/fetch_plugins.py

say "building and starting (first build takes a few minutes)"
"${COMPOSE[@]}" "${CARGS[@]}" up -d --build

say "waiting for the stack"
WEB_PORT=$(grep -oP '^PYRRHULA_WEB_PORT=\K.*' .env 2>/dev/null || echo 5173)
for _ in $(seq 1 60); do
  # The readiness probe must touch the DATABASE, not just the process: a login with
  # bogus credentials answers 401/422 when the stack (incl. migrations) is healthy,
  # 5xx when it is not (observed live: /health green over a broken database).
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    -X POST "http://localhost:${WEB_PORT}/api/auth/login" \
    -H 'Content-Type: application/json' \
    -d '{"email":"readiness-probe@invalid.local","password":"x"}' 2>/dev/null || echo 000)
  # 401/403/422 = bad credentials; 404 = "unknown tenant" on a fresh database.
  # All four prove the request went through the app AND a database lookup.
  case "$code" in
    401|403|404|422)
      # Pre-warm the retrieval models NOW, during install, instead of blocking the
      # user's first knowledge/assistant call for many minutes (a cold in-request
      # download has also been seen wedging on registry rate-limit stalls).
      #
      # No model name appears here on purpose: the deployment's choice lives in its
      # configuration (PYRRHULA_EMBEDDING_MODEL / PYRRHULA_RERANKER_MODEL, see
      # docs/install.md), so the installer asks the app what it is set to and warms
      # that. A hardcoded name here would silently pre-warm the wrong model for anyone
      # who changed it, and then charge them the cold download anyway.
      say "downloading the retrieval models (one-time, please wait)"
      warm='
from core.config import get_settings
from sentence_transformers import CrossEncoder, SentenceTransformer
s = get_settings()
SentenceTransformer(s.embedding_model.split("/", 1)[-1])
if s.reranker_enabled:
    CrossEncoder(s.reranker_model.split("/", 1)[-1])
'
      # Two attempts: container DNS can lag for a few seconds right after first start.
      if ! "$ENGINE" exec pyrrhula_api_1 python -c "$warm" >/dev/null 2>&1; then
        echo "   first attempt failed -- retrying in 20s"
        sleep 20
        if ! "$ENGINE" exec pyrrhula_api_1 python -c "$warm" >/dev/null 2>&1; then
          # Don't leave a mystery: the most common cause is container DNS that answers
          # internal names but cannot forward external ones (rootless podman's
          # aardvark-dns blocked by a host firewall change is the classic case). That
          # breaks far more than this download -- every model-provider call would fail
          # the same way -- so diagnose it now, while someone is watching.
          if "$ENGINE" exec pyrrhula_api_1 python -c \
              'import socket; socket.gethostbyname("huggingface.co")' >/dev/null 2>&1; then
            echo "   warning: pre-download failed (network reachable; likely transient or a"
            echo "            huggingface outage). The first knowledge/assistant call will"
            echo "            fetch the model instead."
          else
            echo "   ERROR: containers cannot resolve external DNS (internal service names"
            echo "          work, huggingface.co does not). Until this is fixed, model"
            echo "          downloads AND every cloud model call will fail from this stack."
            echo "          This is host container-networking, not Pyrrhula: on rootless"
            echo "          podman it is usually aardvark-dns forwarding blocked by a"
            echo "          firewall change. Quick fix -- pin public DNS for this stack:"
            echo "            cat > docker/compose.override.yml <<'EOF'"
            echo "            services:"
            echo "              api:    {dns: [1.1.1.1, 8.8.8.8]}"
            echo "              worker: {dns: [1.1.1.1, 8.8.8.8]}"
            echo "            EOF"
            echo "            $ENGINE compose -p pyrrhula -f docker/compose.selfhost.yml -f docker/compose.override.yml up -d api worker"
            echo "          then re-run: ./install.sh compose"
          fi
        fi
      fi
      say "up."
      echo
      echo "  Open   http://localhost:${WEB_PORT}  and Sign up."
      echo "  Admin  http://localhost:$(grep -oP '^PYRRHULA_ADMIN_PORT=\K.*' .env 2>/dev/null || echo 8100)  (token: grep ADMIN_TOKEN .env)"
      echo "  Next   add a model connection (Connections page), then launch a session."
      echo "  Docs   docs/install.md (post-install, TLS, upgrades, troubleshooting)"
      exit 0
      ;;
  esac
  sleep 5
done
fail "stack did not become healthy in 5 minutes -- check: ${COMPOSE[*]} -p $PROJECT -f docker/compose.selfhost.yml logs"

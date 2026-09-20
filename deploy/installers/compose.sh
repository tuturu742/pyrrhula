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

# Read KEY=value out of an env file, with a fallback. POSIX sed rather than `grep -oP`:
# -P is a GNU extension, so on macOS's BSD grep every one of these silently produced an
# empty string -- wrong ports in the printed URLs, and blank admin credentials on the
# fresh machine that most needs them.
envval() { # envval <file> <key> <default>
  _v=$(sed -n "s/^$2=//p" "$1" 2>/dev/null | head -1)
  if [ -n "$_v" ]; then printf '%s' "$_v"; else printf '%s' "$3"; fi
}

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
PYRRHULA_ADMIN_EMAIL=admin@example.com
PYRRHULA_ADMIN_PASSWORD=$(openssl rand -hex 12)
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

# Top-up for stacks created before the installer generated an admin login: without these
# the admin console can only be reached through the deprecated token app.
if ! grep -q '^PYRRHULA_ADMIN_EMAIL=' .env; then
  say "adding a platform-admin login to .env"
  {
    echo "PYRRHULA_ADMIN_EMAIL=admin@example.com"
    echo "PYRRHULA_ADMIN_PASSWORD=$(openssl rand -hex 12)"
  } >> .env
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
WEB_PORT=$(envval .env PYRRHULA_WEB_PORT 5173)

# The readiness probe must touch the DATABASE, not just the process: a login with
# bogus credentials answers 401/422 when the stack (incl. migrations) is healthy,
# 5xx when it is not (observed live: /health green over a broken database).
# 401/403/422 = bad credentials; 404 = "unknown tenant" on a fresh database. All four
# prove the request went through the app AND a database lookup.
api_ready() {
  _code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    -X POST "http://localhost:${WEB_PORT}/api/auth/login" \
    -H 'Content-Type: application/json' \
    -d '{"email":"readiness-probe@invalid.local","password":"x"}' 2>/dev/null || echo 000)
  case "$_code" in 401|403|404|422) return 0 ;; *) return 1 ;; esac
}

# Wait, but say WHY when nothing moves. This loop used to print nothing for five
# minutes and then "check the logs" -- the least useful thing an installer can do,
# because the cause is sitting in those logs the whole time. Report it while still
# waiting, then fail with it rather than making the reader go find it.
diagnose() {
  echo "   -- containers"
  "${COMPOSE[@]}" "${CARGS[@]}" ps 2>&1 | sed 's/^/      /'
  echo "   -- migrate (Alembic runs here; the stack stays unhealthy if it failed)"
  "${COMPOSE[@]}" "${CARGS[@]}" logs --tail=12 migrate 2>&1 | sed 's/^/      /'
  echo "   -- api"
  "${COMPOSE[@]}" "${CARGS[@]}" logs --tail=12 api 2>&1 | sed 's/^/      /'
  # The classic cause that is NOT covered by the fresh-.env guard above: an .env that
  # already existed, over a postgres volume initialised with different secrets. Postgres
  # only applies its password on first init, so the two drift apart silently and every
  # query fails auth.
  if "${COMPOSE[@]}" "${CARGS[@]}" logs --tail=80 api migrate 2>&1 \
     | grep -qi 'password authentication failed'; then
    echo "   -- the database is rejecting the app's password"
    echo "      postgres only applies a password on FIRST init, so an .env whose secrets"
    echo "      no longer match the existing volume can never connect. Restore the .env"
    echo "      this data was created with (backup: ~/.config/pyrrhula/compose.env.bak),"
    echo "      or wipe the old install:"
    echo "        ${COMPOSE[*]} -p $PROJECT -f docker/compose.selfhost.yml down -v"
  fi
  if "${COMPOSE[@]}" "${CARGS[@]}" logs --tail=80 web api 2>&1 \
     | grep -qiE 'address already in use|bind: permission denied'; then
    echo "   -- a port is already taken"
    echo "      Something else holds ${WEB_PORT}. Set PYRRHULA_WEB_PORT in .env and re-run."
  fi
}

deadline=$(( $(date +%s) + 300 )); warned=0
while ! api_ready; do
  now=$(date +%s)
  if [ "$now" -ge "$deadline" ]; then
    echo "ERROR: the stack did not become healthy within 5 minutes."
    diagnose
    exit 1
  fi
  if [ "$warned" -eq 0 ] && [ "$now" -ge "$(( deadline - 255 ))" ]; then
    warned=1
    echo "   still waiting after 45s -- what the stack says right now:"
    diagnose
  fi
  sleep 5
done

# Pre-warm the retrieval models NOW, during install, instead of blocking the user's
# first knowledge/assistant call for many minutes (a cold in-request download has also
# been seen wedging on registry rate-limit stalls).
#
# No model name appears here on purpose: the deployment's choice lives in its
# configuration (PYRRHULA_EMBEDDING_MODEL / PYRRHULA_RERANKER_MODEL, see
# docs/install.md), so the installer asks the app what it is set to and warms that. A
# hardcoded name would silently pre-warm the wrong model for anyone who changed it, and
# then charge them the cold download anyway.
#
# Opt-out: the models are no longer *required* at install time. They can be fetched
# later from Admin -> Retrieval models (a background job), or uploaded there as a cache
# tarball on a box with no route to huggingface.co. Skipping costs a slow first
# knowledge call, nothing more.
if [ "${PYRRHULA_SKIP_MODEL_DOWNLOAD:-0}" = "1" ]; then
  echo "   skipping the retrieval-model download (PYRRHULA_SKIP_MODEL_DOWNLOAD=1)."
  echo "   Fetch them later: Admin -> Retrieval models -> Download, or upload a"
  echo "   cache tarball there. Until then the first knowledge call fetches them."
else
  say "downloading the retrieval models (one-time, please wait; set"
  say "PYRRHULA_SKIP_MODEL_DOWNLOAD=1 to skip and do it from the admin console)"
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
      # internal names but cannot forward external ones (rootless podman's aardvark-dns
      # blocked by a host firewall change is the classic case). That breaks far more
      # than this download -- every model-provider call would fail the same way -- so
      # diagnose it now, while someone is watching.
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
fi

# Once the cache is populated, run offline. An unauthenticated HF-hub check can HANG
# (rate-limit stall, no timeout) inside the in-process model load and wedge the api's
# event loop -- observed on the k8s stack as NotReady for 13+ minutes at idle CPU, which
# is why that installer flips its pods. Compose had the same exposure with the fix
# available only as a comment in the compose file, so it never happened on its own.
#
# Keyed on the cache actually being populated, not on the warm step's exit status: a
# rerun over a wiped volume must be able to download again rather than being pinned
# offline against an empty cache.
HF_SIZE=$("$ENGINE" exec pyrrhula_api_1 du -sm /app/.cache/huggingface 2>/dev/null | cut -f1 || echo 0)
if [ "${HF_SIZE:-0}" -gt 1000 ] && [ "$(envval .env PYRRHULA_HF_OFFLINE 0)" != "1" ]; then
  say "cache warm (${HF_SIZE}MB) -- pinning this stack to offline model loads"
  # `sed -i.bak` + rm, not bare `-i`: BSD sed (macOS) requires the suffix argument.
  if grep -q '^PYRRHULA_HF_OFFLINE=' .env; then
    sed -i.bak 's/^PYRRHULA_HF_OFFLINE=.*/PYRRHULA_HF_OFFLINE=1/' .env && rm -f .env.bak
  else
    echo 'PYRRHULA_HF_OFFLINE=1' >> .env
  fi
  # The running containers still hold the old value; .env alone changes nothing until
  # they are recreated. Only api and worker load models, so leave the rest alone.
  "${COMPOSE[@]}" "${CARGS[@]}" up -d --no-deps api worker >/dev/null 2>&1 || true
  echo "   waiting for the api to come back"
  back=$(( $(date +%s) + 120 ))
  while ! api_ready; do
    if [ "$(date +%s)" -ge "$back" ]; then
      echo "   warning: the api did not answer within 2 minutes of the offline switch."
      diagnose
      break
    fi
    sleep 5
  done
fi

say "up."
echo
echo "  Open   http://localhost:${WEB_PORT}"
echo
echo "  Sign in as the platform admin:"
echo "    organization  admin"
echo "    email         $(envval .env PYRRHULA_ADMIN_EMAIL '(not set)')"
echo "    password      $(envval .env PYRRHULA_ADMIN_PASSWORD '(not set)')"
echo "  (generated on first run, stored in .env; change the password IN THE APP"
echo "   after first login -- editing .env afterwards does not rotate it)"
echo
echo "  Or Sign up to create your own organization."
echo "  Legacy token console (deprecated): http://localhost:$(envval .env PYRRHULA_ADMIN_PORT 8100)  (token: grep ADMIN_TOKEN .env)"
echo "  Next   add a model connection (Connections page), then launch a session."
echo "  Docs   docs/install.md (post-install, TLS, upgrades, troubleshooting)"

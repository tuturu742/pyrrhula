#!/usr/bin/env bash
# Compose installer: docker or podman, one machine, smallest footprint.
# Idempotent -- rerun after `git pull` to upgrade. `--check` verifies prereqs only.
#
#   deploy/installers/compose.sh [--single-tenant|--multi-tenant] [--check]
set -euo pipefail
cd "$(dirname "$0")/../.."

say()  { printf '\033[1m== %s\033[0m\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Tenancy is chosen here, not in a config file somebody has to find afterwards.
# Single is the default because that is what a self-hosted install almost always is:
# one person or one team, one organization, and no reason to type its name at every
# login. `--multi-tenant` is the same build with the pre-auth shortcut off -- it is a
# flag over one multi-tenant core, never a different product, and switching later is an
# env change plus a restart (rerun this installer with the other flag).
CHECK_ONLY=0
PURGE=0
SINGLE_TENANT=true
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --purge) PURGE=1 ;;
    --single-tenant) SINGLE_TENANT=true ;;
    --multi-tenant)  SINGLE_TENANT=false ;;
    *) fail "unknown flag $arg (want: --single-tenant | --multi-tenant | --check | --purge)" ;;
  esac
done

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

if [ "$CHECK_ONLY" = 1 ]; then
  say "prerequisites OK"; exit 0
fi

# Explicit project name: without it, compose derives the project from the compose
# FILE's directory ("docker"), which collides across checkouts -- observed live as a
# fresh install silently mounting a stale postgres volume whose passwords no longer
# matched the fresh .env.
PROJECT="${PYRRHULA_COMPOSE_PROJECT:-pyrrhula}"
CARGS=(-p "$PROJECT" -f docker/compose.selfhost.yml)

if [ "$PURGE" = 1 ]; then
  # Everything this installer ever created: containers, networks, and the named volumes
  # holding postgres, blobs and the downloaded retrieval model. `.env` goes too -- it
  # carries the generated passwords for the database that is being deleted, and keeping
  # it means the next install writes fresh credentials into a file the stale ones are
  # still in, which is the one way to get a deployment that looks installed and cannot
  # authenticate to its own database.
  say "purging the '$PROJECT' deployment (containers, volumes, generated .env)"
  "${COMPOSE[@]}" "${CARGS[@]}" down -v --remove-orphans 2>/dev/null || true
  # That `down` needs the compose file to parse, and the compose file needs a .env: on a
  # fresh checkout (or after .env was lost) it exits non-zero having removed nothing, and
  # the volumes it was meant to delete survive to fail the next step. Found by doing
  # exactly that. Finish by name -- every container, volume and network this project
  # creates carries the project prefix -- and refuse to continue if the database
  # volume is still there, because "purged" has to mean purged.
  "$ENGINE" ps -a --format '{{.Names}}' | while read -r name; do
    case "$name" in "${PROJECT}_"*) "$ENGINE" rm -f "$name" >/dev/null ;; esac
  done
  "$ENGINE" volume ls --format '{{.Name}}' | while read -r vol; do
    case "$vol" in "${PROJECT}_"*) "$ENGINE" volume rm -f "$vol" >/dev/null ;; esac
  done
  "$ENGINE" network ls --format '{{.Name}}' | while read -r net; do
    case "$net" in "${PROJECT}_"*) "$ENGINE" network rm -f "$net" >/dev/null 2>&1 || true ;; esac
  done
  if "$ENGINE" volume exists "${PROJECT}_pyrrhula-postgres" 2>/dev/null \
     || "$ENGINE" volume inspect "${PROJECT}_pyrrhula-postgres" >/dev/null 2>&1; then
    fail "purge could not remove volume ${PROJECT}_pyrrhula-postgres -- is a container still using it?"
  fi
  rm -f docker/.env .env
  say "purged -- installing fresh"
fi

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

# Tenancy, written every run rather than only at generation: rerunning with the other
# flag is how a deployment switches, so the flag has to win over what is already there.
# The app reads this once at startup, so the recreate below is what makes it take effect.
if grep -q '^PYRRHULA_SINGLE_TENANT_UI=' .env; then
  CURRENT=$(envval .env PYRRHULA_SINGLE_TENANT_UI true)
  if [ "$CURRENT" != "$SINGLE_TENANT" ]; then
    say "switching to $([ "$SINGLE_TENANT" = true ] && echo single || echo multi)-tenant"
    sed -i.bak "s/^PYRRHULA_SINGLE_TENANT_UI=.*/PYRRHULA_SINGLE_TENANT_UI=$SINGLE_TENANT/" .env
    rm -f .env.bak
  fi
else
  echo "PYRRHULA_SINGLE_TENANT_UI=$SINGLE_TENANT" >> .env
fi

# Top-up for stacks created before the installer generated an admin login: without these
# there is no way to sign in as the platform admin at all.
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
# A container that cannot resolve a name is not obviously a DNS problem from inside the
# product: the model download reports "couldn't connect to huggingface.co", which reads
# as an outage. The common cause on this shape of host is systemd-resolved -- the host's
# only nameserver is 127.0.0.53, a loopback address that means nothing in a container's
# namespace, and podman's DNS forwards there. Say so now rather than at the first
# download, and say what to do about it.
# Rather than tell the operator to fix it, use the resolver systemd-resolved itself
# forwards to: /run/systemd/resolve/resolv.conf lists the real upstreams (k3s does the
# same). Link-local IPv6 entries carry a scope a container cannot use, so only IPv4 is
# taken; with nothing usable, a public resolver and a note. PYRRHULA_COMPOSE_DNS set by
# the operator always wins.
if [ -z "${PYRRHULA_COMPOSE_DNS:-}" ] && grep -qs '^nameserver 127\.' /etc/resolv.conf; then
  UPSTREAM=$(sed -n 's/^nameserver \([0-9][0-9.]*\)$/\1/p' /run/systemd/resolve/resolv.conf 2>/dev/null | head -1)
  if [ -n "$UPSTREAM" ]; then
    export PYRRHULA_COMPOSE_DNS="$UPSTREAM"
    echo "   note: this host resolves DNS through a loopback address (127.0.0.x), which a"
    echo "         container cannot reach; using its upstream resolver $UPSTREAM inside the"
    echo "         deployment instead (set PYRRHULA_COMPOSE_DNS to choose another)."
  else
    export PYRRHULA_COMPOSE_DNS="1.1.1.1"
    echo "   note: this host resolves DNS through a loopback address (127.0.0.x) and names"
    echo "         no upstream; using 1.1.1.1 inside the deployment (set PYRRHULA_COMPOSE_DNS"
    echo "         to choose another)."
  fi
fi

say "fetching workflow plugins (deploy/plugins.json)"
python3 scripts/fetch_plugins.py

say "building and starting (first build takes a few minutes)"
if [ -n "${PYRRHULA_COMPOSE_DNS:-}" ]; then
  # compose has no portable "set a resolver" switch, so this goes in as an override file
  # rather than being edited into the shipped compose file.
  # Every container that talks to something outside the deployment. `searxng` is on this
  # list because it is the one that talks to the most: it was left off, so on a host
  # needing this override the search engine could not resolve a single upstream and every
  # web search returned zero results with HTTP 200 -- a silent nothing, not an error, so
  # agent web search looked like a model that never searched rather than a broken
  # resolver.
  cat > docker/compose.dns.yml <<YAML
services:
  api:     { dns: [ "${PYRRHULA_COMPOSE_DNS}" ] }
  worker:  { dns: [ "${PYRRHULA_COMPOSE_DNS}" ] }
  migrate: { dns: [ "${PYRRHULA_COMPOSE_DNS}" ] }
  searxng: { dns: [ "${PYRRHULA_COMPOSE_DNS}" ] }
YAML
  CARGS+=(-f docker/compose.dns.yml)
  say "using DNS ${PYRRHULA_COMPOSE_DNS} inside the containers"
fi

"${COMPOSE[@]}" "${CARGS[@]}" up -d --build

# `up --build` builds the new image and then, depending on the compose implementation,
# happily leaves the old container running on the old one. Observed here: an image built
# thirty seconds ago beside a container started half an hour earlier, serving packs that
# had been replaced -- an install that reports success and ships yesterday's code.
#
# Recreate the services that carry application code. The data services are deliberately
# left alone: postgres and redis hold the deployment's state, restarting them costs
# every open connection, and neither has code in this image.
say "recreating application containers so they run the image just built"
"${COMPOSE[@]}" "${CARGS[@]}" up -d --force-recreate --no-deps api worker web

say "waiting for the stack"
WEB_PORT=$(envval .env PYRRHULA_WEB_PORT 5173)

# The readiness probe must touch the DATABASE, not just the process: a login with
# bogus credentials answers 401/404/422 when the stack (incl. migrations) is healthy,
# 5xx when it is not (observed live: /health green over a broken database).
# 401/403/422 = bad credentials; 404 = "unknown tenant" -- all four prove the request
# went through the app AND a database lookup.
#
# The tenant header is sent deliberately, with a slug nothing can own. Without it the
# probe's result depends on the tenancy mode: multi-tenant answers 400 "header
# required" -- raised before any query, so it would not prove a database lookup even if
# it were accepted -- and the install would wait five minutes and fail on a stack that
# was healthy the whole time. It passed before only because single-tenant mode was on
# and answered 404 for a tenant slug no installer creates, which is to say the probe was
# resting on a bug.
api_ready() {
  _code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    -X POST "http://localhost:${WEB_PORT}/api/auth/login" \
    -H 'Content-Type: application/json' \
    -H 'X-Pyrrhula-Tenant: readiness-probe-no-such-tenant' \
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

# The retrieval models are NOT downloaded here. That used to be the slowest part of an
# install by a wide margin -- multiple gigabytes, before the operator had seen a single
# screen -- to fetch a model nobody had yet chosen. Which embedding model a deployment
# runs is a decision its operator makes, so it belongs where decisions are made: Admin
# console -> Models, which can download in the background (a worker job, restartable) or
# take an uploaded cache tarball on a box with no route to Hugging Face.
#
# Nothing breaks meanwhile. Knowledge ingests and chunks without a model; only semantic
# search waits, and the app says so rather than stalling on a silent fetch.

# --- does this deployment actually work? ------------------------------------------
# Mandatory, and deliberately so: "installed" has to mean more than "the API answers".
# The readiness gate above proves the app reached the database; this proves a document
# survives the whole loop -- blob write, queue, a *second* process claiming it, a parse,
# a row. The failure modes that cost the most time -- a worker OOMing on its first job,
# a queue whose leases were never reclaimed, a blob volume mounted read-only -- all pass
# the readiness gate and all fail here. Pack-free and model-free, so it means the same
# thing on every install, including one that will only ever run the swdev workflow.
say "verifying the deployment"
if ! "$ENGINE" exec pyrrhula_api_1 python /app/deploy-smoke.py; then
  echo
  echo "ERROR: the stack is up but cannot do real work. Details above."
  diagnose
  exit 1
fi

say "up."
echo
echo "  Open   http://localhost:${WEB_PORT}"
echo
# Single-tenant needs no credentials at all: signing up creates the one organization
# and makes you its owner, which on this shape of deployment is also the platform
# admin. Leading with a generated password would be telling someone to use an account
# that is not theirs, on their own machine.
if [ "$SINGLE_TENANT" = true ]; then
  echo "  Register. You will be the owner of this deployment's one organization and"
  echo "  its administrator -- no organization name to type, and nothing to copy from"
  echo "  here. Re-run with --multi-tenant to host several organizations instead."
  echo
  echo "  Next   App settings -> Models (a tab in your own navigation): choose and"
  echo "         download the retrieval models (needed for semantic search), then add"
  echo "         a model connection under Personas -> Model profiles."
  echo
  echo "  Locked out? A break-glass platform admin exists: organization 'admin',"
  echo "  $(envval .env PYRRHULA_ADMIN_EMAIL '(not set)') / $(envval .env PYRRHULA_ADMIN_PASSWORD '(not set)') (also in .env)."
else
  echo "  Register to create an organization -- every sign-in names its organization."
  echo
  echo "  Administer the deployment (tenants, models, plugins) as the platform admin:"
  echo "    organization  admin"
  echo "    email         $(envval .env PYRRHULA_ADMIN_EMAIL '(not set)')"
  echo "    password      $(envval .env PYRRHULA_ADMIN_PASSWORD '(not set)')"
  echo "  (generated on first run, stored in .env; change the password IN THE APP"
  echo "   after first login -- editing .env afterwards does not rotate it)"
  echo
  echo "  Next   Admin -> Models: choose and download the retrieval models (needed"
  echo "         for semantic search), then add a model connection under Personas ->"
  echo "         Model profiles."
fi
echo "  Docs   docs/install.md (post-install, TLS, upgrades, troubleshooting)"

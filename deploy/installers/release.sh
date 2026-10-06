#!/bin/sh
# Install a published Pyrrhula release on this machine. No checkout, no build, no
# Python or Node on the host -- one compose file and three pulled images.
#
#   curl -fsSL https://raw.githubusercontent.com/tuturu742/pyrrhula/main/deploy/installers/release.sh | sh
#   curl -fsSL .../release.sh | sh -s -- 0.1.1              # a specific release
#
# Everything lands in ./pyrrhula (override with PYRRHULA_DIR). Rerun it to upgrade:
# the .env is kept, the compose file and images are refreshed.
#
# This is deliberately the small path. From a checkout, deploy/installers/compose.sh
# does the same thing with more diagnosis (`./install.sh compose --from-registry`), and
# everything it knows about a stack that will not start applies here too -- see
# docs/install.md.
set -eu

REPO="tuturu742/pyrrhula"
# Used only when GitHub cannot be asked (offline, rate-limited) and no version was given.
FALLBACK_VERSION="0.1.1"
VERSION="${1:-${PYRRHULA_VERSION:-}}"
if [ -z "$VERSION" ]; then
  # The newest *stable* release: GitHub's releases/latest skips pre-releases, so the
  # one-liner on main never needs editing when a release ships.
  VERSION=$(curl -fsSL --max-time 15 "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
    | sed -n 's/.*"tag_name": *"v\{0,1\}\([^"]*\)".*/\1/p' | head -1)
  VERSION="${VERSION:-$FALLBACK_VERSION}"
fi
DIR="${PYRRHULA_DIR:-./pyrrhula}"
# Names every container, volume and network in this deployment. Change it (with
# PYRRHULA_WEB_PORT and PYRRHULA_API_PORT) to stand a release beside an existing
# Pyrrhula on the same host rather than on top of it.
PROJECT="${PYRRHULA_COMPOSE_PROJECT:-pyrrhula}"

say()  { printf '\033[1m== %s\033[0m\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# --- prerequisites ---------------------------------------------------------------
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"; ENGINE=docker
elif command -v podman >/dev/null && podman compose version >/dev/null 2>&1; then
  COMPOSE="podman compose"; ENGINE=podman
elif command -v podman-compose >/dev/null; then
  COMPOSE="podman-compose"; ENGINE=podman
else
  fail "need docker (with the compose plugin) or podman with a compose provider"
fi
command -v curl >/dev/null    || fail "need curl"
command -v openssl >/dev/null || fail "need openssl (it generates this deployment's secrets)"
say "engine: $ENGINE ($COMPOSE), release: $VERSION, project: $PROJECT"

# --- the compose file ------------------------------------------------------------
mkdir -p "$DIR"
cd "$DIR"
# Both files, from the same tag. The settings are SearXNG's, not ours to republish as
# an image, and the compose file bind-mounts them by name -- a stack missing this file
# exits 127 rather than running a search that answers with nothing.
say "downloading the compose file and search settings for $VERSION"
for f in compose.release.yml searxng-settings.yml; do
  curl -fsSL -o "$f" "https://raw.githubusercontent.com/$REPO/v$VERSION/docker/$f" \
    || fail "no $f for v$VERSION -- check the tag at https://github.com/$REPO/releases"
done

# --- secrets ---------------------------------------------------------------------
# Generated once and never regenerated: postgres only applies its password on first
# init, so fresh secrets over existing data produce a stack that starts and cannot
# authenticate to its own database.
if [ ! -f .env ]; then
  say "generating .env (secrets)"
  {
    echo "PYRRHULA_VERSION=$VERSION"
    echo "PYRRHULA_COMPOSE_PROJECT=$PROJECT"
    echo "PYRRHULA_POSTGRES_PASSWORD=$(openssl rand -hex 24)"
    echo "PYRRHULA_APP_DB_PASSWORD=$(openssl rand -hex 24)"
    echo "PYRRHULA_JWT_SECRET=$(openssl rand -base64 48 | tr -d '\n')"
    echo "PYRRHULA_ENCRYPTION_KEY=$(openssl rand -base64 32)"
    echo "PYRRHULA_ADMIN_EMAIL=admin@example.com"
    echo "PYRRHULA_ADMIN_PASSWORD=$(openssl rand -hex 12)"
    echo "PYRRHULA_SINGLE_TENANT_UI=true"
    # Ports given to this run are part of the deployment, not of this one shell: a
    # later `compose up` from the directory must publish the same ones, and the URL
    # printed below must name them. Without this a release stood beside another on
    # 5373 came up there and announced itself on 5173 (sweep, 2026-10-05).
    [ -n "${PYRRHULA_WEB_PORT:-}" ] && echo "PYRRHULA_WEB_PORT=$PYRRHULA_WEB_PORT"
    [ -n "${PYRRHULA_API_PORT:-}" ] && echo "PYRRHULA_API_PORT=$PYRRHULA_API_PORT"
  } > .env
  # Delegated coding agents build and test in sibling containers, which needs the host
  # engine's socket. Absent is not fatal -- it disables that one feature.
  if [ "$ENGINE" = docker ] && [ -S /var/run/docker.sock ]; then
    echo "PYRRHULA_ENGINE_SOCKET=/var/run/docker.sock" >> .env
  elif [ -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock" ]; then
    echo "PYRRHULA_ENGINE_SOCKET=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock" >> .env
  else
    echo "   note: no container socket found -- delegated coding agents stay disabled."
    echo "         rootless podman: systemctl --user enable --now podman.socket"
  fi
  chmod 600 .env
  mkdir -p "$HOME/.config/pyrrhula"
  cp .env "$HOME/.config/pyrrhula/release.env.bak"
  chmod 600 "$HOME/.config/pyrrhula/release.env.bak"
else
  say "using the existing .env (upgrading in place)"
  if grep -q '^PYRRHULA_VERSION=' .env; then
    sed -i.bak "s/^PYRRHULA_VERSION=.*/PYRRHULA_VERSION=$VERSION/" .env && rm -f .env.bak
  else
    # An .env from before the version was recorded in it: record both now.
    echo "PYRRHULA_VERSION=$VERSION" >> .env
    echo "PYRRHULA_COMPOSE_PROJECT=$PROJECT" >> .env
  fi
fi

# --- start -----------------------------------------------------------------------
say "pulling images"
$COMPOSE -p "$PROJECT" -f compose.release.yml pull

say "starting"
$COMPOSE -p "$PROJECT" -f compose.release.yml up -d

# A one-shot service that already exited is left alone by `up`, so an upgrade would run
# new code against the old schema. `run` always starts a fresh container from the image
# now in place and returns its exit code.
# `< /dev/null` is load-bearing, not tidiness. This script is meant to be run as
# `curl ... | sh`, which puts the script itself on stdin; `compose run` attaches stdin
# to the container by default, so without this the migrate container reads the rest of
# this file, the shell hits EOF early, and the install stops silently right here --
# after the stack is up, before it ever prints the URL or the admin password. Observed
# doing exactly that.
say "running migrations"
$COMPOSE -p "$PROJECT" -f compose.release.yml run --rm --no-deps migrate < /dev/null

WEB_PORT=$(sed -n 's/^PYRRHULA_WEB_PORT=//p' .env | head -1); WEB_PORT="${WEB_PORT:-5173}"
say "waiting for the stack"
# The probe must touch the database, not just the process: /health answers ok over a
# broken database. A login with credentials nothing owns answers 401/403/404/422 only
# once the request has reached the app AND a database lookup.
i=0
until [ "$i" -ge 60 ]; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    -X POST "http://localhost:${WEB_PORT}/api/auth/login" \
    -H 'Content-Type: application/json' \
    -H 'X-Pyrrhula-Tenant: readiness-probe-no-such-tenant' \
    -d '{"email":"readiness-probe@invalid.local","password":"x"}' 2>/dev/null || echo 000)
  case "$code" in 401|403|404|422) break ;; esac
  i=$((i + 1))
  sleep 5
done
if [ "$i" -ge 60 ]; then
  echo "   the stack did not come up. What it says for itself:"
  $COMPOSE -p "$PROJECT" -f compose.release.yml ps        2>&1 | sed 's/^/      /'
  $COMPOSE -p "$PROJECT" -f compose.release.yml logs --tail=20 migrate api 2>&1 | sed 's/^/      /'
  fail "not ready after five minutes (see above; docs/install.md has the common causes)"
fi

# The version the api reports, not the one we asked for: on an upgrade those differ
# until the new containers are actually serving, and the second is the one worth
# printing. Pull the field out rather than echoing the whole probe body at someone.
VERSION_RUNNING=$(curl -fsS "http://localhost:${WEB_PORT}/api/health" 2>/dev/null \
  | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')
# The same end-to-end check every installer runs (docs/install.md, "Does it work?"): a
# blob write, a queued job the *worker* claims and runs, rows in the database. A worker
# that dies on its first job looks healthy to the probe above and fails here.
say "checking the stack end to end"
$COMPOSE -p "$PROJECT" -f compose.release.yml exec -T api python /app/deploy-smoke.py < /dev/null \
  || fail "the post-install check failed (output above); the stack is up but not working"

say "ready"
ADMIN_EMAIL=$(sed -n 's/^PYRRHULA_ADMIN_EMAIL=//p' .env)
ADMIN_PASSWORD=$(sed -n 's/^PYRRHULA_ADMIN_PASSWORD=//p' .env)
echo
echo "   Pyrrhula ${VERSION_RUNNING:-$VERSION} is running."
echo
echo "   Open http://localhost:${WEB_PORT} and finish in the browser:"
echo "     1. Register, naming your organization. The first account owns this"
echo "        deployment and is its admin."
echo "     2. App settings -> Models -> Download from Hugging Face. About 6.5 GB, once;"
echo "        until it finishes, sessions run but cannot search their knowledge."
echo "     3. Personas -> Model profiles -> New model profile, with your provider's API key."
echo "     4. Import a sample: https://github.com/tuturu742/pyrrhula-samples"
echo
echo "   Files     $(pwd)   (.env holds this deployment's secrets)"
echo "   Backup    ~/.config/pyrrhula/release.env.bak"
echo "             BACK UP the PYRRHULA_ENCRYPTION_KEY line: losing it orphans every"
echo "             stored credential -- provider keys, repository tokens, registry passwords."
echo "   Spare admin (only if locked out): organization 'admin', $ADMIN_EMAIL / $ADMIN_PASSWORD"
echo
echo "   Stop      $COMPOSE -p $PROJECT -f compose.release.yml stop     (from $(pwd))"
echo "   Upgrade   rerun this script"

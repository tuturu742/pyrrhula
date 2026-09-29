#!/bin/sh
# Install a published Pyrrhula release on this machine. No checkout, no build, no
# Python or Node on the host -- one compose file and three pulled images.
#
#   curl -fsSL https://raw.githubusercontent.com/tuturu742/pyrrhula/main/deploy/installers/release.sh | sh
#   curl -fsSL .../release.sh | sh -s -- 0.1.0-rc1        # a specific release
#
# Everything lands in ./pyrrhula (override with PYRRHULA_DIR). Rerun it to upgrade:
# the .env is kept, the compose file and images are refreshed.
#
# This is deliberately the small path. From a checkout, deploy/installers/compose.sh
# does the same thing with more diagnosis (`./install.sh compose --from-registry`), and
# everything it knows about a stack that will not start applies here too -- see
# docs/install.md.
set -eu

VERSION="${1:-${PYRRHULA_VERSION:-0.1.0-rc1}}"
DIR="${PYRRHULA_DIR:-./pyrrhula}"
REPO="tuturu742/pyrrhula"

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
say "engine: $ENGINE ($COMPOSE), release: $VERSION"

# --- the compose file ------------------------------------------------------------
mkdir -p "$DIR"
cd "$DIR"
say "downloading the compose file for $VERSION"
curl -fsSL -o compose.release.yml \
  "https://raw.githubusercontent.com/$REPO/v$VERSION/docker/compose.release.yml" \
  || fail "no compose file for v$VERSION -- check the tag at https://github.com/$REPO/releases"

# --- secrets ---------------------------------------------------------------------
# Generated once and never regenerated: postgres only applies its password on first
# init, so fresh secrets over existing data produce a stack that starts and cannot
# authenticate to its own database.
if [ ! -f .env ]; then
  say "generating .env (secrets)"
  {
    echo "PYRRHULA_VERSION=$VERSION"
    echo "PYRRHULA_POSTGRES_PASSWORD=$(openssl rand -hex 24)"
    echo "PYRRHULA_APP_DB_PASSWORD=$(openssl rand -hex 24)"
    echo "PYRRHULA_JWT_SECRET=$(openssl rand -base64 48 | tr -d '\n')"
    echo "PYRRHULA_ENCRYPTION_KEY=$(openssl rand -base64 32)"
    echo "PYRRHULA_ADMIN_EMAIL=admin@example.com"
    echo "PYRRHULA_ADMIN_PASSWORD=$(openssl rand -hex 12)"
    echo "PYRRHULA_SINGLE_TENANT_UI=true"
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
    echo "PYRRHULA_VERSION=$VERSION" >> .env
  fi
fi

# --- start -----------------------------------------------------------------------
say "pulling images"
$COMPOSE -p pyrrhula -f compose.release.yml pull

say "starting"
$COMPOSE -p pyrrhula -f compose.release.yml up -d

# A one-shot service that already exited is left alone by `up`, so an upgrade would run
# new code against the old schema. `run` always starts a fresh container from the image
# now in place and returns its exit code.
say "running migrations"
$COMPOSE -p pyrrhula -f compose.release.yml run --rm --no-deps migrate

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
  $COMPOSE -p pyrrhula -f compose.release.yml ps        2>&1 | sed 's/^/      /'
  $COMPOSE -p pyrrhula -f compose.release.yml logs --tail=20 migrate api 2>&1 | sed 's/^/      /'
  fail "not ready after five minutes (see above; docs/install.md has the common causes)"
fi

VERSION_RUNNING=$(curl -fsS "http://localhost:${WEB_PORT}/api/health" 2>/dev/null || echo '')
say "ready"
echo
echo "   Pyrrhula ${VERSION_RUNNING:-$VERSION}"
echo "   open     http://localhost:${WEB_PORT}"
echo "   admin    $(sed -n 's/^PYRRHULA_ADMIN_EMAIL=//p' .env) / $(sed -n 's/^PYRRHULA_ADMIN_PASSWORD=//p' .env)"
echo "   config   $(pwd)/.env   (backup: ~/.config/pyrrhula/release.env.bak)"
echo
echo "   BACK UP the PYRRHULA_ENCRYPTION_KEY line. Losing it orphans every stored"
echo "   credential -- provider keys, repository tokens, registry passwords."
echo
echo "   First start downloads the ~2.2GB retrieval model into a volume; until it"
echo "   finishes, sessions run but retrieve nothing. Watch it with:"
echo "     $COMPOSE -p pyrrhula -f compose.release.yml logs -f worker"

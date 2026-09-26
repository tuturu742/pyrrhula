# Self-hosting Pyrrhula

> **Start with [docs/install.md](install.md)** — `./install.sh compose` automates
> everything below (secrets, engine socket, compose up, health wait). This page keeps
> the manual walkthrough and the operational reference.

Everything runs from one compose file. Prereqs: podman (rootless is fine) or docker,
with the compose plugin; ~4 GB RAM for the stack itself (models are extra).

## Quickstart

```bash
git clone <this repo> && cd pyrrhula
cp docker/env.example .env
# Fill in the four required secrets:
#   openssl rand -base64 48 -> PYRRHULA_JWT_SECRET
#   openssl rand -base64 32 -> PYRRHULA_ENCRYPTION_KEY (exactly 32 decoded bytes)
#   openssl rand -hex 24 -> the two database passwords
$EDITOR .env

# Exec environments (agents building/testing code in containers) need the engine socket:
systemctl --user enable --now podman.socket        # podman hosts
# docker hosts: set PYRRHULA_ENGINE_SOCKET=/var/run/docker.sock in .env

podman compose -f docker/compose.selfhost.yml up -d --build
```

Then open http://localhost:5173 (UI). Platform admin lives in the same UI: sign in with
organization `admin`, using `PYRRHULA_ADMIN_EMAIL` / `PYRRHULA_ADMIN_PASSWORD`.

**Set both in `.env` before first start.** The account is bootstrapped only when both are
present, so a manual quickstart that leaves them blank comes up with no platform admin and
no way to become one. (`./install.sh` generates and prints them for you; this hand-rolled
path does not — that is the difference between the two routes above.)
Change the password in the app after first login; the account is bootstrapped once and
editing `.env` later does not rotate it.

Nothing is downloaded at first start: fetch the retrieval models under **Admin → Models**
(they land in the `pyrrhula-hf` volume). `PYRRHULA_HF_OFFLINE` defaults to `1`.

**Models**: agents need at least one model connection (**Personas → Model profiles**): a
cloud key (OpenAI / Anthropic / Gemini / any OpenAI-compatible endpoint) or a local
Ollama. For the latter, uncomment the `ollama` service and the `pyrrhula-ollama` volume
at the bottom of `docker/compose.selfhost.yml`, `compose up -d ollama`, pull a model
(`podman exec ollama ollama pull <model>`), and point the connection at
`http://ollama:11434`.

**Usage limits**: organization owners can set daily hard caps on model tokens —
per organization, per connection, per persona, per user — on the Organization page → "Daily
usage limits" (API: `GET/PUT /limits`). 0 = unlimited. At the cap, sessions pause
with the reason and assistant calls return 429 until midnight UTC. This is the
platform-side backstop for provider API bills.

## Kubernetes

`deploy/k8s/` runs the whole stack on any cluster (built against single-node k3s):
kustomize base + dev overlay, `dev-up.sh` one-command install, and an
exec-engine story where delegated coding agents run as one-shot **Jobs** in an
isolated namespace. The engine can also be used standalone against a cluster while
the app stays on compose. See `deploy/k8s/README.md`.

## TLS

The stack terminates TLS itself with the compose overlay — bring certificates
(certbot's, a corporate CA's, or a self-signed pair for a lab):

```bash
PYRRHULA_TLS_CERT_DIR=/etc/letsencrypt/live/pyrrhula.example.com \
PYRRHULA_TLS_SERVER_NAME=pyrrhula.example.com \
docker compose -f docker/compose.selfhost.yml -f docker/compose.tls.yml up -d
```

That swaps nginx onto a 443 listener (HSTS on, http 301s to https) with your mounted
`fullchain.pem`/`privkey.pem`, and sets `PYRRHULA_COOKIE_SECURE=true` on the api so the
session cookie stops travelling in the clear. Certificates are never baked into an
image. A lab pair is one command:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout privkey.pem -out fullchain.pem -subj "/CN=localhost"
```

Prefer an existing reverse proxy? Point it at the `web` service instead (Caddy gets
Let's Encrypt automatically) and set `PYRRHULA_COOKIE_SECURE=true` on the api yourself:

```
# Caddyfile
pyrrhula.example.com {
    reverse_proxy localhost:5173
}
```

Either way: the web container already proxies `/api/*` internally with streaming-safe
settings, so one upstream is enough. If remote exec engines (k8s/cloud) are declared,
also expose the API's `/git/*` path at a routable URL and set `PYRRHULA_GIT_HTTP_BASE`
to it (job-token auth; safe to expose). Kubernetes TLS is cert-manager based — see
`deploy/k8s/README.md`.

## Desktop-first

The web app is a desktop operator console by design: canvases (process editor, repo
graph), tables, and the conduct surface assume a pointer and ~900px+ of width. It
degrades gracefully on tablets (the header scrolls rather than wraps) but is not a
phone app.

## Operations

- **Backups**: `podman exec pyrrhula_postgres_1 pg_dump -U pyrrhula pyrrhula > backup.sql`
  plus the `pyrrhula-blobs` volume (hosted git repos + uploads). The `pyrrhula-hf`
  volume is a re-downloadable cache.
- **Upgrades**: `git pull && podman compose -f docker/compose.selfhost.yml up -d
  --build` — the `migrate` one-shot runs Alembic before api/worker start.
- **Credential sealing**: everything written after setup is AES-256-GCM sealed. If you
  ever ran without `PYRRHULA_ENCRYPTION_KEY` (dev), seal legacy rows once:
  `podman exec pyrrhula_api_1 python -m worker.reencrypt --yes`.
  Losing the key means stored provider keys/tokens must be re-entered — back it up.
- **Password reset** (no email delivery in v1):
  `podman exec pyrrhula_api_1 python -m worker.reset_password <tenant-slug> <email>`
  prints a fresh password once.
- **Health**: `curl localhost:8000/health` for liveness, and
  `podman exec pyrrhula_api_1 python /app/deploy-smoke.py` for the real question — the
  same check the installer runs, which drives a document through the queue and the
  worker. Exec-engine options in `docs/exec-engines.md`.

## The admin assistant

The admin console has an **Assistant** page. Point it at a model under **Models**
(provider, model, API key — a connection on the reserved admin organization, so no
tenant's budget pays for console questions), then ask about the deployment: whether the retrieval models are downloaded,
which plugin repositories are registered, how many tenants exist.

It can also prepare changes — switching the embedding model, queueing a model download,
registering a plugin repository. **It proposes; it never applies.** A proposal arrives as
a card with an Apply button, and that click calls the ordinary admin endpoint from your
own browser session. The assistant holds no privilege of its own, so it cannot change this
deployment any more than you can, and what it does lands in the same audit trail as if you
had clicked it yourself.

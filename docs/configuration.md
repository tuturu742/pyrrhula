# Configuration reference

Every environment variable Pyrrhula reads, and what it is for. Environment variables are
for *deployment facts* — where the database is, which socket the engine listens on, what
this host can reach. Anything about how an organization or a workspace works is a setting
in the product; the last section lists those.

`.env` is read by Docker Compose, for the variables `docker/compose.selfhost.yml`
interpolates; the processes themselves read only their environment, so a variable the
compose file does not pass through has to be added to its env block. The rest are read
directly where they are used.

---

## Connection and identity

| Variable | Default | What it does |
|---|---|---|
| `PYRRHULA_DATABASE_URL` | `postgresql+asyncpg://pyrrhula:pyrrhula@localhost:5432/pyrrhula` | Owner connection: migrations and admin work. |
| `PYRRHULA_APP_DATABASE_URL` | `postgresql+asyncpg://pyrrhula_app:pyrrhula_app_dev@localhost:5432/pyrrhula` | The **application** role's connection — the one RLS actually constrains. An independent default, *not* derived from `PYRRHULA_DATABASE_URL`. Never the owner in production. |
| `PYRRHULA_APP_DB_PASSWORD` | — | Password the migration path grants the app role. Read by the deployment (compose, CI), not by `Settings`. |
| `PYRRHULA_POSTGRES_PASSWORD` | generated | Compose/k8s only: seeds the database's own password on first boot. |
| `PYRRHULA_REDIS_URL` | `redis://localhost:6379/0` | Cache, rate-limit counters, and the live event bus. |
| `PYRRHULA_DB_POOL_SIZE` | `5` | Pooled connections per engine. **`0` selects `NullPool`** — nothing is pooled and every session opens and closes its own connection. Worth it only where engines are short-lived: the test run sets it, because an engine rebinds per event loop and a pooled connection belonging to an abandoned engine cannot be closed from the loop that notices, so a long run exhausts Postgres. |
| `PYRRHULA_DB_MAX_OVERFLOW` | `10` | Connections an engine may open beyond the pool under load. Ignored when `PYRRHULA_DB_POOL_SIZE` is `0`. Setting it to `0` alongside a pool of 1 deadlocks anything needing two sessions at once — don't. |
| `PYRRHULA_JWT_SECRET` | — (**required** by compose and k8s; a bare process falls back to an insecure dev default and warns) | Signs session tokens. Rotating it logs everyone out. |
| `PYRRHULA_JWT_ALGORITHM` | `HS256` | Token signing algorithm. |
| `PYRRHULA_COOKIE_SECURE` | `false` | Set `true` behind HTTPS so the session cookie is never sent in clear. |
| `PYRRHULA_ENCRYPTION_KEY` | — | AES-GCM key for secrets and stored credentials. **Required on every container recreate** — without it, previously encrypted data cannot be read. |
| `PYRRHULA_REQUIRE_ENCRYPTION` | unset | Refuses to boot with the identity (no-op) encryptor. Set it in production so a misconfigured deployment fails loudly instead of storing plaintext. |

## Tenancy and access

| Variable | Default | What it does |
|---|---|---|
| `PYRRHULA_SINGLE_TENANT_UI` | `false` (but **`true` in compose and k8s**) | Solo/self-host mode: a request with no tenant header resolves to this deployment's single organization. |
| `PYRRHULA_DEFAULT_TENANT_SLUG` | — (inferred) | Pins single-tenant mode to one tenant slug. Leave unset: with exactly one organization, that one is used. Set it only where several exist and one should be the default. |
| `PYRRHULA_ALLOW_TENANT_SIGNUP` | `true` | What a fresh deployment starts with for self-serve organisation signup. The admin console's **Self-serve signup** switch overrides it once touched — a policy is flipped at runtime, not by redeploying. |
| `PYRRHULA_ADMIN_EMAIL` / `PYRRHULA_ADMIN_PASSWORD` | generated | The platform admin created on first boot. Changing them afterwards does **not** rotate the account — change the password in the app. |
| `PYRRHULA_RATE_LIMIT_REQUESTS` | `300` | Per-IP request ceiling per window. |
| `PYRRHULA_RATE_LIMIT_TENANT_REQUESTS` | `1200` | Per-tenant ceiling per window. |
| `PYRRHULA_RATE_LIMIT_WINDOW_SECONDS` | `60` | The window both ceilings apply to. |

## Execution, previews and repos

| Variable | Default | What it does |
|---|---|---|
| `PYRRHULA_WEB_SEARCH_URL` | unset | SearXNG endpoint backing the `web_search` tool. The installers run one beside the api and point this at it; a workspace that wants its own instance edits the `web_search` server's URL in its MCP registry instead. |
| `PYRRHULA_EXEC_ENGINES` | unset | Registry of execution engines (podman socket, k8s, and the experimental `aws-ecs`) available for delegated work. |
| `PYRRHULA_EXEC_SOCKET` / `PYRRHULA_ENGINE_SOCKET` | unset | Container socket an engine drives. |
| `PYRRHULA_MCP_GIT_ROOT` | `/app/data/blobs/repos` | Where server-side git repositories live. |
| `PYRRHULA_GIT_HTTP_BASE` | `http://pyrrhula_api_1:8000` | Base URL agents clone from over smart-HTTP. Must be reachable **from inside a job container**, which is why it is not the public URL. |
| `PYRRHULA_PUBLIC_BASE_URL` | unset | The URL humans use. Links in notifications and previews. |
| `PYRRHULA_BLOB_STORE_ROOT` | `/app/data/blobs` | Local blob storage path, used when no S3 bucket is configured. |
| `PYRRHULA_BLOB_S3_BUCKET` | unset | **Setting this selects the S3 blob adapter** instead of local disk. |
| `PYRRHULA_BLOB_S3_ENDPOINT` | unset | MinIO/R2/Spaces base URL. Empty means real AWS S3. |
| `PYRRHULA_BLOB_S3_REGION` | `us-east-1` | Bucket region. |
| `PYRRHULA_BLOB_S3_PREFIX` | unset | Key prefix, so one bucket can host several deployments. |
| `PYRRHULA_PREVIEW_IMAGE` | `docker.io/library/python:3.12-slim` | Image serving preview artifacts. Fully qualified on purpose: podman prompts on an unqualified name instead of assuming Docker Hub. |
| `PYRRHULA_PREVIEW_MAX_TTL_SECONDS` | `86400` | Ceiling on any preview's lifetime. The default lifetime is an organization preference beneath this. |
| `PYR_ARTIFACT_URL` / `PYR_ARTIFACT_TOKEN` | injected | Set **by** Pyrrhula inside a preview container so it can fetch its own artifact. Never set these yourself. |

## Plugins

| Variable | Default | What it does |
|---|---|---|
| `PYRRHULA_PLUGIN_DROP_DIR` | `/app/plugins-local` | Folder watched for hand-placed plugin packs — the credential-free install path. |
| `PYRRHULA_PLUGINS_STRICT` | unset | `1` makes a failed plugin fetch a build failure. Without it the build falls back to whatever is cached on disk and the install reports success with the previous pack inside it. What CI should use. |
| `PYRRHULA_COMPOSE_DNS` | derived | A nameserver for the compose containers, e.g. `1.1.1.1`. On a host whose only resolver is `systemd-resolved` at `127.0.0.53` — a loopback address that means nothing inside a container namespace — the installer derives the host's upstream resolver itself; set this to choose another. |

## Observability

| Variable | Default | What it does |
|---|---|---|
| `PYRRHULA_OTEL_EXPORTER_ENDPOINT` | unset | OTLP collector endpoint. Unset disables export. |
| `PYRRHULA_OTEL_DEBUG` | unset | Prints spans to stdout. Noisy; for debugging only. |

## Ceilings

Bounds the operator imposes on every organization. Defaults are the supported
configuration.

| Variable | Default | What it does |
|---|---|---|
| `PYRRHULA_REVIEW_ROUNDS_CEILING` | `10` | Hard ceiling on review→rework cycles. The *number of rounds* is a workspace setting (`max_review_rounds`); this is only the bound a workspace cannot exceed, because an unbounded review loop spends a tenant's API budget in a cycle nobody watched. |

## Container image

Baked into the image; override only if you know why.
`PYTHONPATH=/app/packages`, `PYTHONUNBUFFERED=1`, `TIKTOKEN_CACHE_DIR`, `HF_HOME`,
`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`. The last three pin the embedding model to a
baked cache — **if `HF_HOME` does not point at the cache volume, turns fail silently**
after a container recreate.

## Serving and TLS

Compose-level only: these shape how the web container is published, not how the
application behaves.

| Variable | Default | What it does |
|---|---|---|
| `PYRRHULA_WEB_PORT` | `5173` | Host port the web container publishes (plain compose); `80` is the container port it maps to. |
| `PYRRHULA_HTTP_PORT` | `80` | Host HTTP port when serving TLS (`compose.tls.yml`). |
| `PYRRHULA_HTTPS_PORT` | `443` | Host HTTPS port. |
| `PYRRHULA_TLS_CERT_DIR` | — (**required for TLS**) | Directory holding `fullchain.pem` and `privkey.pem`, mounted read-only. |
| `PYRRHULA_TLS_SERVER_NAME` | `localhost` | Server name nginx serves the certificate for. |
| `PYRRHULA_API_PORT` | `8000` | Host port the API is published on in the self-host compose file. |
| `PYRRHULA_VERSION` | the release the compose file ships with | Release-images path only (`docker/compose.release.yml`): the image tag every service pulls. Changing it and re-running `pull` + `up -d` + `run --rm migrate` is an upgrade. Ignored by the build-from-source stack, which runs what it built. |
| `PYRRHULA_HF_OFFLINE` | `1` (`0` in `compose.release.yml`) | Retrieval models load strictly from the shared cache; the runtime never reaches Hugging Face on its own. A model that is not there is refused with a message pointing at Admin → Models, rather than fetched mid-request — an unauthenticated hub check has no timeout and has wedged the API's event loop. An admin-console download lifts this for that fetch alone. The release-images stack defaults to `0` instead, because a freshly pulled deployment has an empty cache and nothing to load strictly from. |

## Installer and verification only

Never read by the running product.

| Variable | What it does |
|---|---|
| `PYRRHULA_SMOKE_WORKER_TIMEOUT` | Seconds the installer's post-install check waits for the worker to run its test job (default `90`). |
| `PYRRHULA_K8S_REGISTRY` | Push images to a registry instead of importing into k3s — the no-sudo install path. |
| `PYRRHULA_COMPOSE_PROJECT`, `PYRRHULA_WEB_PORT`, `PYRRHULA_API_UPSTREAM` | Compose naming and ports. |
| `PYRRHULA_DIR` | Where `deploy/installers/release.sh` puts the compose file and `.env` (default `./pyrrhula`). |

---

## Settings in the product

Anything two organizations might reasonably want different is a setting, not an
environment variable. Workspace settings override organization settings, which override
the built-in default; a key that is absent inherits, a key that is present is used as
stored — so clearing an override means removing it, and the UI shows the inherited value
as the placeholder.

| Setting | Where | Default | What it does |
|---|---|---|---|
| `session_lifetime_seconds` | Organization page | 24 h | how long a login lasts |
| `preview_ttl_seconds` | Organization page | 4 h | default preview lifetime, under `PYRRHULA_PREVIEW_MAX_TTL_SECONDS` |
| `reranker_enabled` | Organization page | on | whether this organization's retrieval uses the deployment's reranker |
| daily usage limits | Organization page | unlimited | token caps per organization, connection, persona and user |
| `secret_mode`, `conduct_rules` | workspace | `excluded` | how secrets are handled; see [portability.md](portability.md) |
| `max_review_rounds` | workspace | 2 | review→rework cycles, under `PYRRHULA_REVIEW_ROUNDS_CEILING` |
| `moderation_model` | workspace, then organization | none | the classifier that screens authored content; unset means none |
| `assistant_context_max_tokens` | workspace | 6000 | retrieved knowledge the assistant may put in front of the model per question |
| `assistant_class_ratios` | workspace | rules .35 / lore .40 / misc .25 | how that budget splits across knowledge classes |
| `generation_limits` | admin console, per organization | 300 s / 100 000 chars | circuit breakers on one generation; unparseable values read as the default |
| egress policy | admin console, per organization | permissive | which provider kinds each purpose may reach |
| gate connection | workspace secrets card | the persona's own | the model that runs the disclosure gate |
| retrieval models | Admin → Models | `BAAI/bge-m3`, `BAAI/bge-reranker-v2-m3` | deployment-wide; applied on the next restart |
| self-serve signup | Admin → Tenants | on | whether strangers may create an organization |
| joining policy | Admin → Tenants → Joining | `closed` | who may join an existing organization |

`assistant_context_max_tokens` matters more than it looks: every source file a repository
ingest produces lands in `misc`, so a workspace with a codebase attached wants a larger
budget, and `assistant_class_ratios` decides what a class is worth (a code-heavy
workspace wants something like `rules .2 / lore .2 / misc .6`). `priority_weight` on a
knowledge attachment is a different lever: it ranks *sources* against each other, and
cannot say "code matters more than prose".

# Configuration reference

Every environment variable Pyrrhula reads, what it is for, and — where it matters — why
it is an environment variable rather than something you set in the product.

**The rule this list is audited against:** an environment variable is for *deployment
facts* — where the database is, which socket the engine listens on, what this host can
reach. It is **not** for policy about how a workspace plays. Policy belongs where the
person making the decision is already looking: a field on a form, a setting on a
workspace, a number in a flow. A limit that a user has to discover as an env var is a
limit they will never set. (This is why the sample forensic lab keeps no budget of its
own: see `max_calls_per_session` below.)

Anything with the `PYRRHULA_` prefix and a matching field in `core/config.py` can also go
in a `.env` file; the rest are read directly where they are used.

---

## Connection and identity

| Variable | Default | What it does |
|---|---|---|
| `PYRRHULA_DATABASE_URL` | `postgresql+asyncpg://pyrrhula:pyrrhula@localhost:5432/pyrrhula` | Owner connection: migrations and admin work. |
| `PYRRHULA_APP_DATABASE_URL` | derived from the above | The **application** role's connection — the one RLS actually constrains. Never the owner in production. |
| `PYRRHULA_APP_DB_PASSWORD` | — | Password for the app role, used when the app URL is derived rather than given. |
| `PYRRHULA_POSTGRES_PASSWORD` | generated | Compose/k8s only: seeds the database's own password on first boot. |
| `PYRRHULA_REDIS_URL` | `redis://localhost:6379/0` | Cache, rate-limit counters, and the live event bus. |
| `PYRRHULA_JWT_SECRET` | — (**required**) | Signs session tokens. Rotating it logs everyone out. |
| `PYRRHULA_JWT_ALGORITHM` | `HS256` | Token signing algorithm. |
| `PYRRHULA_JWT_EXPIRY_SECONDS` | `86400` | How long a login lasts. |
| `PYRRHULA_COOKIE_SECURE` | `false` | Set `true` behind HTTPS so the session cookie is never sent in clear. |
| `PYRRHULA_AUTH_PROVIDER` | `local` | Which `IdentityProvider` adapter to use. |
| `PYRRHULA_ENCRYPTION_KEY` | — | AES-GCM key for secrets and stored credentials. **Required on every container recreate** — without it, previously encrypted data cannot be read. |
| `PYRRHULA_REQUIRE_ENCRYPTION` | unset | Refuses to boot with the identity (no-op) encryptor. Set it in production so a misconfigured deployment fails loudly instead of storing plaintext. |

## Tenancy and access

| Variable | Default | What it does |
|---|---|---|
| `PYRRHULA_ISOLATION_MODE` | `shared` | `shared` (RLS) or stricter per-tenant isolation. |
| `PYRRHULA_SINGLE_TENANT_UI` | `false` | Solo/self-host mode: no tenant header required, `default_tenant_slug` is assumed. |
| `PYRRHULA_DEFAULT_TENANT_SLUG` | `dev` | The tenant assumed when the UI is single-tenant. |
| `PYRRHULA_ALLOW_TENANT_SIGNUP` | `true` | Whether strangers can create their own organisation. |
| `PYRRHULA_ADMIN_EMAIL` / `PYRRHULA_ADMIN_PASSWORD` | generated | The platform admin created on first boot. Changing them afterwards does **not** rotate the account — change the password in the app. |
| `PYRRHULA_ADMIN_TOKEN` | generated | Legacy admin console token (deprecated). |
| `PYRRHULA_ADMIN_PORT` | `8100` | Port for that legacy console. |
| `PYRRHULA_RATE_LIMIT_REQUESTS` | `300` | Per-IP request ceiling per window. |
| `PYRRHULA_RATE_LIMIT_TENANT_REQUESTS` | `1200` | Per-tenant ceiling per window. |
| `PYRRHULA_RATE_LIMIT_WINDOW_SECONDS` | `60` | The window both ceilings apply to. |

## Models and retrieval

| Variable | Default | What it does |
|---|---|---|
| `PYRRHULA_EMBEDDING_MODEL` | `local/BAAI/bge-m3` | Embedding model for knowledge retrieval. |
| `PYRRHULA_EMBEDDING_DIMENSION` | `1024` | Vector width. Must match the model **and** the existing index — changing it needs a re-embed. |
| `PYRRHULA_RERANKER_ENABLED` | `true` | Whether retrieved chunks are reranked. |
| `PYRRHULA_RERANKER_MODEL` | `local/BAAI/bge-reranker-v2-m3` | The reranker. |
| `PYRRHULA_GATE_MODEL` / `PYRRHULA_GATE_API_BASE` | unset | Model for the secret-disclosure gate. Unset means the workspace's own connection. |
| `PYRRHULA_MODERATION_MODEL` / `PYRRHULA_MODERATION_API_BASE` | unset | Model for the moderation provider. |
| `PYRRHULA_ASSISTANT_MODEL` / `PYRRHULA_ASSISTANT_API_BASE` | unset | Model for the workspace assistant. **Leave unset on a clean install** — the deployment should assume nothing about what models a user has. |
| `PYRRHULA_WEB_SEARCH_URL` | unset | SearXNG endpoint backing the `web_search` MCP preset. |
| `PYRRHULA_WEB_SEARCH_ENGINES` | `bing` | Which engines that SearXNG instance should query. |

## Execution, previews and repos

| Variable | Default | What it does |
|---|---|---|
| `PYRRHULA_EXEC_ENGINES` | unset | Registry of execution engines (podman socket, k8s, AWS) available for delegated work. |
| `PYRRHULA_EXEC_SOCKET` / `PYRRHULA_ENGINE_SOCKET` | unset | Container socket an engine drives. |
| `PYRRHULA_MCP_GIT_ROOT` | `/app/data/blobs/repos` | Where server-side git repositories live. |
| `PYRRHULA_GIT_HTTP_BASE` | `http://pyrrhula_api_1:8000` | Base URL agents clone from over smart-HTTP. Must be reachable **from inside a job container**, which is why it is not the public URL. |
| `PYRRHULA_PUBLIC_BASE_URL` | unset | The URL humans use. Links in notifications and previews. |
| `PYRRHULA_BLOB_STORE_ROOT` | `/app/data/blobs` | Local blob storage path, used when no S3 bucket is configured. |
| `PYRRHULA_BLOB_S3_BUCKET` | unset | **Setting this selects the S3 blob adapter** instead of local disk. |
| `PYRRHULA_BLOB_S3_ENDPOINT` | unset | MinIO/R2/Spaces base URL. Empty means real AWS S3. |
| `PYRRHULA_BLOB_S3_REGION` | `us-east-1` | Bucket region. |
| `PYRRHULA_BLOB_S3_PREFIX` | unset | Key prefix, so one bucket can host several deployments. |
| `PYRRHULA_PREVIEW_IMAGE` | `python:3.12-slim` | Image serving preview artifacts. |
| `PYRRHULA_PREVIEW_TTL_SECONDS` | `14400` | Default preview lifetime. |
| `PYRRHULA_PREVIEW_MAX_TTL_SECONDS` | `86400` | Ceiling a requester may ask for. |
| `PYR_ARTIFACT_URL` / `PYR_ARTIFACT_TOKEN` | injected | Set **by** Pyrrhula inside a preview container so it can fetch its own artifact. Never set these yourself. |

## Plugins

| Variable | Default | What it does |
|---|---|---|
| `PYRRHULA_PLUGIN_DROP_DIR` | `/app/plugins-local` | Folder watched for hand-placed plugin packs — the credential-free install path. |
| `PYRRHULA_PLUGINS_TOKEN` / `GH_TOKEN` | unset | Token for fetching plugin repos that are private. A clean install must never need one. |

## Observability

| Variable | Default | What it does |
|---|---|---|
| `PYRRHULA_OTEL_EXPORTER_ENDPOINT` | unset | OTLP collector endpoint. Unset disables export. |
| `PYRRHULA_OTEL_DEBUG` | unset | Prints spans to stdout. Noisy; for debugging only. |

## Adapter escape hatches

These exist so a deployment that hits a wall has a lever, not because anyone is expected
to set them. Defaults are the supported configuration.

| Variable | Default | What it does |
|---|---|---|
| `PYRRHULA_OLLAMA_NUM_CTX` | `16384` | Per-request context window for Ollama. Its default of 4096 makes real prompts return **empty generations silently**, which is why this is forced. |
| `PYRRHULA_EMPTY_RETRY_TOKEN_FACTOR` | `4` | When a reasoning model burns its whole budget and returns nothing, the retry multiplies `max_tokens` by this — keeping the caller's reasoning level. |
| `PYRRHULA_REASONING_MIN_COMPLETION_TOKENS` | `2048` | Floor for that retry. |
| `PYRRHULA_HISTORY_CHAR_BUDGET` | `24000` | Ceiling on transcript characters fed to a turn, above the flow's own budget. |
| `PYRRHULA_CODEGEN_MAX_TOKENS` | `12000` | Completion budget for the codegen tool, which writes whole files. |
| `PYRRHULA_MAX_REVIEW_ROUNDS` | `2` | How many review→rework cycles a delegated work item may chain before stopping. ⚠️ **Audit flag** — see below. |

### Still flagged

**`PYRRHULA_MAX_REVIEW_ROUNDS` is workflow policy.** How many times a reviewer may send
work back is the same kind of decision as how many rounds an interrogation runs — and that
one lives in the flow, as declared state and a gate. Two tenants doing different work
would want different values, so by the rule above it should be a workspace or flow
setting, not an environment variable.

*(`PYRRHULA_REMOTE_MCP_TIMEOUT_S` and `PYRRHULA_REMOTE_MCP_MAX_RESULT_CHARS` were also
flagged here and have since been **removed**: both are now fields on the MCP server's
registration, alongside `calls/session`.)*

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
| `PYRRHULA_WEB_PORT` | `80` | Host port the web container publishes (plain compose). |
| `PYRRHULA_HTTP_PORT` | `80` | Host HTTP port when serving TLS (`compose.tls.yml`). |
| `PYRRHULA_HTTPS_PORT` | `443` | Host HTTPS port. |
| `PYRRHULA_TLS_CERT_DIR` | — (**required for TLS**) | Directory holding `fullchain.pem` and `privkey.pem`, mounted read-only. |
| `PYRRHULA_TLS_SERVER_NAME` | `localhost` | Server name nginx serves the certificate for. |
| `PYRRHULA_API_PORT` | `8000` | Host port the API is published on in the self-host compose file. |
| `PYRRHULA_HF_OFFLINE` | `1` | Set `0` to let the image download embedding weights instead of using the baked cache. Needed only on a first run with no cache. |

## Installer and verification only

Never read by the running product.

| Variable | What it does |
|---|---|
| `PYRRHULA_K8S_REGISTRY` | Push images to a registry instead of importing into k3s — the no-sudo install path. |
| `PYRRHULA_COMPOSE_PROJECT`, `PYRRHULA_WEB_PORT`, `PYRRHULA_API_UPSTREAM` | Compose naming and ports. |
| `PYRRHULA_VERIFY_*` | Targets for `docs/deploy-verification.md` scripts (API URL, exec sockets, model names, MCP sidecars). |
| `DEEPSEEK_KEY` | A one-off demo script's provider key. Never used by the product. |

---

## What is deliberately *not* an environment variable

These were considered and put somewhere a user will actually find them:

- **How long to wait on an external MCP server, how much of its answer to accept, and how
  many times a session may call it** — `timeout_seconds`, `max_result_chars` and
  `max_calls_per_session`, all fields on the server's registration. An env var would be global, and the external
  server cannot enforce it per session at all (it is never told which session is calling).
- **Sampling parameters** (`temperature`, `seed`, `presence_penalty`, `reasoning_effort`) —
  on the connection, and per persona in the persona editor. One connection serves models
  with different knobs.
- **Which knowledge a persona may retrieve** — scope bands and knowledge classes, in the
  workspace and the flow.
- **How secrets are handled** — `secret_mode` on the workspace; it also travels in a `.pyr`.
- **How many rounds a discussion runs** — the flow's own state and gates.

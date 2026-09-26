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
| `PYRRHULA_APP_DATABASE_URL` | `postgresql+asyncpg://pyrrhula_app:pyrrhula_app_dev@localhost:5432/pyrrhula` | The **application** role's connection — the one RLS actually constrains. An independent default, *not* derived from `PYRRHULA_DATABASE_URL`. Never the owner in production. |
| `PYRRHULA_APP_DB_PASSWORD` | — | Password the migration path grants the app role. Read by the deployment (compose, CI), not by `Settings`. |
| `PYRRHULA_POSTGRES_PASSWORD` | generated | Compose/k8s only: seeds the database's own password on first boot. |
| `PYRRHULA_REDIS_URL` | `redis://localhost:6379/0` | Cache, rate-limit counters, and the live event bus. |
| `PYRRHULA_DB_POOL_SIZE` | `5` | Pooled connections per engine. **`0` selects `NullPool`** — nothing is pooled and every session opens and closes its own connection. Worth it only where engines are short-lived: the test run sets it, because an engine rebinds per event loop and a pooled connection belonging to an abandoned engine cannot be closed from the loop that notices, so a long run exhausts Postgres. |
| `PYRRHULA_DB_MAX_OVERFLOW` | `10` | Connections an engine may open beyond the pool under load. Ignored when `PYRRHULA_DB_POOL_SIZE` is `0`. Setting it to `0` alongside a pool of 1 deadlocks anything needing two sessions at once — don't. |
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
| `PYRRHULA_SINGLE_TENANT_UI` | `false` (but **`true` in compose and k8s**) | Solo/self-host mode: a request with no tenant header resolves to this deployment's single organization. |
| `PYRRHULA_DEFAULT_TENANT_SLUG` | — (inferred) | Pins single-tenant mode to one tenant slug. Leave unset: with exactly one organization, that one is used. Set it only where several exist and one should be the default. |
| `PYRRHULA_ALLOW_TENANT_SIGNUP` | `true` | Whether strangers can create their own organisation. |
| `PYRRHULA_DEFAULT_REGISTRATION_POLICY` | `closed` | What an organisation that has not chosen gets for **joining an existing** one (a different question from creating a new one). `closed` — nobody self-registers; `request` — anyone may apply and an admin approves; `open` — anyone who knows the organisation name gets a viewer account. Each organisation overrides it in the admin console under **Joining**. Anything unrecognised is read as `closed`. |
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
| `PYRRHULA_GATE_MODEL` / `PYRRHULA_GATE_API_BASE` | unset | Deployment-wide **default** model for the secret-disclosure gate. A tenant that picks one of its own connections (Admin → gate model) overrides this; unset and unchosen means the gate runs on the acting persona's model. |
| `PYRRHULA_MODERATION_MODEL` / `PYRRHULA_MODERATION_API_BASE` | unset | Deployment **default** for the moderation classifier. A workspace or tenant that sets `moderation_model` overrides it; unset everywhere means content is not screened. |
| `PYRRHULA_ASSISTANT_MODEL` / `PYRRHULA_ASSISTANT_API_BASE` | unset | Model for the workspace assistant. **Leave unset on a clean install** — the deployment should assume nothing about what models a user has. |
| `PYRRHULA_WEB_SEARCH_URL` | unset | SearXNG endpoint backing the `web_search` MCP preset. |

## Execution, previews and repos

| Variable | Default | What it does |
|---|---|---|
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
| `PYRRHULA_PREVIEW_TTL_SECONDS` | `14400` | Default preview lifetime. |
| `PYRRHULA_PREVIEW_MAX_TTL_SECONDS` | `86400` | Ceiling a requester may ask for. |
| `PYR_ARTIFACT_URL` / `PYR_ARTIFACT_TOKEN` | injected | Set **by** Pyrrhula inside a preview container so it can fetch its own artifact. Never set these yourself. |

## Plugins

| Variable | Default | What it does |
|---|---|---|
| `PYRRHULA_PLUGIN_DROP_DIR` | `/app/plugins-local` | Folder watched for hand-placed plugin packs — the credential-free install path. |
| `PYRRHULA_PLUGINS_TOKEN` / `GH_TOKEN` | unset | Token for fetching plugin repos that are private. A clean install must never need one. |
| `PYRRHULA_PLUGINS_STRICT` | `0` | `1` makes a failed plugin fetch a build failure. Without it the build falls back to whatever is cached on disk and the install reports success with the previous pack inside it. What CI should use. |
| `PYRRHULA_COMPOSE_DNS` | unset | A nameserver for the compose containers, e.g. `1.1.1.1`. Needed on a host whose only resolver is `systemd-resolved` at `127.0.0.53` — a loopback address that means nothing inside a container namespace, so every outbound lookup fails and the symptom is an apparent Hugging Face outage. |
| `PYRRHULA_OLLAMA_BASE` | unset | Base URL for a local Ollama, for a deployment that seats personas on one. No sample does. |

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
| `PYRRHULA_REVIEW_ROUNDS_CEILING` | `10` | Hard ceiling on review→rework cycles. The *number of rounds* is a workspace setting (`max_review_rounds`); this is only the bound a workspace cannot exceed, because an unbounded review loop spends a tenant's API budget in a cycle nobody watched. |

### Resolved since the audit

- `PYRRHULA_REMOTE_MCP_TIMEOUT_S`, `PYRRHULA_REMOTE_MCP_MAX_RESULT_CHARS` → fields on the
  MCP server's registration, beside `calls/session`.
- `PYRRHULA_MAX_REVIEW_ROUNDS` → `max_review_rounds`, a workspace setting resolved through
  the chain; only the ceiling stays in the environment.
- `PYRRHULA_EMPTY_RETRY_TOKEN_FACTOR`, `PYRRHULA_REASONING_MIN_COMPLETION_TOKENS` →
  plain constants. No tenant has a reason to want a different multiplier for "the model
  reasoned past its allowance", and something nobody should vary is not configuration.
- `PYRRHULA_GATE_MODEL`, `PYRRHULA_GATE_API_BASE` → **kept, and correctly layered.** They
  looked dead (the gate has run on a tenant-chosen connection for some time) and were
  briefly deleted; `scripts/check_env_docs.py` caught it. They are read through
  `getattr(settings, "gate_model", "")`, so removing them degrades silently instead of
  raising — a deployment's gate would quietly fall back to each persona's own model. They
  are the system default at the bottom of the chain, which is exactly where they belong.

- `PYRRHULA_MODERATION_MODEL` → resolved per tenant/workspace, **without touching the
  port**. `ModerationProvider.check()` still knows nothing about tenants; the composition
  root resolves which adapter to build, which is where a selection decision belongs.
- `PYRRHULA_WEB_SEARCH_ENGINES` → `options.engines` on the `web_search` registration.
  Which engines an instance can actually use is a fact about that instance.
- `PYRRHULA_HISTORY_CHAR_BUDGET`, `PYRRHULA_CODEGEN_MAX_TOKENS` → connection/persona
  params (`history_char_budget`, `max_tokens`). Both were justified in code comments by
  *which model on what hardware*, which is the definition of per-connection.

### Still open

Nothing. Every variable below is a deployment fact, a system default beneath a resolution
chain, or the documented exception.

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
| `PYRRHULA_HF_OFFLINE` | `1` | Retrieval models load strictly from the shared cache; the runtime never reaches Hugging Face on its own. A model that is not there is refused with a message pointing at Admin → Models, rather than fetched mid-request — an unauthenticated hub check has no timeout and has wedged the API's event loop. An admin-console download lifts this for that fetch alone. |

## Installer and verification only

Never read by the running product.

| Variable | What it does |
|---|---|
| `PYRRHULA_SMOKE_WORKER_TIMEOUT` | Seconds the installer's post-install check waits for the worker to run its test job (default `90`). |
| `PYRRHULA_K8S_REGISTRY` | Push images to a registry instead of importing into k3s — the no-sudo install path. |
| `PYRRHULA_COMPOSE_PROJECT`, `PYRRHULA_WEB_PORT`, `PYRRHULA_API_UPSTREAM` | Compose naming and ports. |
| `DEEPSEEK_KEY` | A one-off demo script's provider key. Never used by the product. |

---

## How a setting resolves

Values that two tenants might reasonably disagree about are settings, not environment
variables, and they resolve through one chain:

```
workspace.settings  ->  tenant.settings  ->  the deployment default (below)
```

**Absent means inherit; present means chosen.** A key that is not in a settings dict falls
through; a key that is there is used exactly as stored, *including* `0`, `false` and `""`.
That distinction is load-bearing: a workspace has to be able to turn something off
deliberately, so "off" and "not set here" cannot be spelled the same way.

Consequently, **clearing an override means removing the key, not saving an empty value.**
In the UI a blank field inherits, and saving it blank deletes the override rather than
storing a zero the resolver would honour. The placeholder shows the value being inherited,
so an empty box is never ambiguous.

The same shape the vocabulary overlay has always used
(`core/vocabulary/service.py`), implemented once in `core/settings/resolve.py` so no call
site re-derives it and quietly disagrees about which layer wins.

Settings that use it today: `secret_mode`, `conduct_rules`, `allow_automerge`,
`max_review_rounds`, `moderation_model`, `assistant_context_max_tokens`,
`assistant_class_ratios`.

`assistant_context_max_tokens` is how many tokens of retrieved workspace knowledge the
assistant may put in front of the model on one question, across `/assist` and the chat
widget alike. It defaults to **6000**.

The default matters more than it looks. The budget is split across knowledge classes
(`rules` 0.35, `lore` 0.40, `misc` 0.25) and *every source file a repository ingest
produces lands in `misc`* — so the class holding the most material gets the smallest
share. At the old 2400 the `misc` bucket was 600 tokens, about two chunks, and a question
about how code fits together was answered from a single chunk of one file. Unused budget
does spill between classes, but it spills in tokens, and a leftover smaller than one chunk
buys nothing.

Raise it for a workspace with a codebase attached; lower it for a model with a genuinely
small context window. It is a workspace-level property, not a platform one: a six-crate
repository and a one-page handbook do not want the same number.

`assistant_class_ratios` is that split itself, as a map (default
`{"rules": 0.35, "lore": 0.40, "misc": 0.25}`). Ratios need not sum to 1; they are
normalised. A workspace whose knowledge is mostly code wants something like
`{"rules": 0.2, "lore": 0.2, "misc": 0.6}`. A setting that is not a usable map, or whose
values total zero, is ignored in favour of the default -- a zero total would give every
class a zero budget, which reads as "the assistant stopped finding anything" rather than as
a bad setting.

**This is not the same lever as `priority_weight`,** and the difference decides which one
to reach for. `workspace_knowledge_attachment.priority_weight` scales an attached *source*,
so it says "this handbook matters more here than that one". It cannot say "code matters
more than prose", because a repository is a single source whose entries span all three
classes -- weighting it raises every class equally, and a uniform weight normalises away to
no change at all. Use `priority_weight` to rank sources against each other, and
`assistant_class_ratios` to change what a class is worth.

## What is deliberately *not* an environment variable

These were considered and put somewhere a user will actually find them:

- **How long one generation may run, and how much it may emit** — `generation_limits`
  on the tenant (`{"max_seconds": 300, "max_chars": 100000}`), loaded per request and
  enforced inside the `ModelProvider` port, exactly like the egress policy beside it.
  Circuit breakers, not budgets: nothing else in the stack catches a model that stops
  stopping. The job lease measures *silence*, and a runaway is not silent — one kept a
  heartbeat alive for 79 minutes while emitting 470,244 characters that then became the
  next turn's prompt. A character ceiling measures *output*, and the worst case emits
  none, because a reasoning model's thinking never reaches content — turns of 22 and 58
  minutes produced nothing at all. So one of each, and both **fail closed**: 0, a
  negative, or anything unparseable reads as the default, never as "no limit", because
  no limit is the state they exist to end.
- **How long to wait on an external MCP server, how much of its answer to accept, and how
  many times a session may call it** — `timeout_seconds`, `max_result_chars` and
  `max_calls_per_session`, all fields on the server's registration. An env var would be global, and the external
  server cannot enforce it per session at all (it is never told which session is calling).
- **Sampling parameters** (`temperature`, `seed`, `presence_penalty`, `reasoning_effort`) —
  documented in [models.md](models.md); in short:
  on the connection, and per persona in the persona editor. One connection serves models
  with different knobs.
- **Which knowledge a persona may retrieve** — scope bands and knowledge classes, in the
  workspace and the flow.
- **How secrets are handled** — `secret_mode` on the workspace; it also travels in a `.pyr`.
- **How many rounds a discussion runs** — the flow's own state and gates.
- **How many times work goes back for rework** — `max_review_rounds` on the workspace.
- **Which classifier screens authored content** — `moderation_model` on the workspace.
- **How much transcript a model is given, and how big a file it may write** —
  `history_char_budget` and `max_tokens` on the connection.

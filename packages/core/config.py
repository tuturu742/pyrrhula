"""Process-wide settings, read once from the environment.

Two database URLs on purpose (plan §12.1, T0.2): ``database_url`` is the migration/admin
connection (table owner, used only by the ``migrate`` entrypoint); ``app_database_url`` is
what every API/worker process uses, connecting as the non-superuser, non-BYPASSRLS
``pyrrhula_app`` role so row-level security actually applies to it. Handing the app
processes the admin URL would make RLS enforcement silently vanish, because a superuser
bypasses RLS regardless of ``FORCE ROW LEVEL SECURITY``.
"""

from __future__ import annotations

import warnings

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PYRRHULA_", extra="ignore")

    database_url: str = "postgresql+asyncpg://pyrrhula:pyrrhula@localhost:5432/pyrrhula"
    app_database_url: str = (
        "postgresql+asyncpg://pyrrhula_app:pyrrhula_app_dev@localhost:5432/pyrrhula"
    )
    redis_url: str = "redis://localhost:6379/0"
    # SQLAlchemy's own defaults, stated rather than inherited, because one process can
    # hold several engines and the ceiling that matters is Postgres's `max_connections`
    # shared across every process pointing at it. Worth turning down where many short
    # lived engines exist -- a test run rebinds the engine per event loop, and fifteen
    # sockets per abandoned pool is what took a full suite to 98 of 100 slots and made
    # whichever test came next fail as if it were flaky.
    # 0 means "pool nothing": every session opens its own connection and closes it on
    # release. That is the only setting that survives an engine being abandoned, which
    # is why the test run uses it -- a pooled connection belonging to an engine whose
    # event loop has gone cannot be closed from the loop that notices, so it sits open
    # until garbage collection, and a long run accumulates them until Postgres refuses.
    db_pool_size: int = 5
    db_max_overflow: int = 10

    auth_provider: str = "local"
    isolation_mode: str = "shared"
    single_tenant_ui: bool = False
    # PYRRHULA_SINGLE_TENANT_UI pins requests to one tenant when no X-Pyrrhula-Tenant
    # header is present (T0.6) -- "the MVP UI exposes one tenant's worth of
    # functionality" is a feature flag over a multi-tenant core (plan §13.8), not a
    # different build.
    #
    # Empty by default, meaning "infer it". This used to default to "dev", a tenant no
    # installer has ever created, while compose and Kubernetes both switch the mode on:
    # out of the box it promised "no tenant header required" and 404'd every header-less
    # login. A deployment with exactly one tenant needs no configuration to say which one
    # it is (see api.middleware.tenant); set this only to pin a specific slug where
    # several exist.
    default_tenant_slug: str = ""

    # Session-cookie hardening: set true wherever the deployment terminates TLS, so the
    # browser refuses to send the session cookie over plain http. Left false by default
    # because the local dev stack is http://pyrrhula.localhost and a Secure cookie there
    # would silently break login rather than protect anything.
    cookie_secure: bool = False

    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expiry_seconds: int = 60 * 60 * 24

    # Legacy ops shortcut into the platform-admin API: a shared bearer token, entirely
    # separate from the per-tenant JWTs above. Empty does NOT stop anything from starting
    # (nothing checks it at startup) -- it disables the shortcut, and every admin request
    # then has to authenticate as an owner/admin of the reserved admin tenant through the
    # normal JWT path (api/routes/admin.py: `if expected and ...`). That is the supported
    # route now; the standalone console it serves is marked deprecated.
    admin_token: str = ""
    # Bootstrap login for the reserved admin tenant (organization "admin" on the normal
    # login form): when both are set, api startup idempotently ensures this owner account
    # exists. Rotating the password here does NOT update an existing account.
    admin_email: str = ""
    admin_password: str = ""
    # SearXNG (or compatible) base URL registered as the workspace `web_search` MCP
    # server when a persona's search switch is first enabled. Empty = the preset's
    # unreachable placeholder (structure without egress).
    web_search_url: str = ""
    # Model-backed moderation (S3/G4.14): full "provider/model" string; empty = the
    # allow-all provider (moderation effectively off beyond per-tenant keyword policy).
    # Deployment-wide DEFAULT gate model, for tenants that have not chosen one of their
    # own connections (core.secrets.gate_config holds that choice, and it wins). Read via
    # getattr in secrets_gate_factory, so deleting these fields degrades silently rather
    # than loudly -- which is exactly how they nearly got removed as "dead".
    gate_model: str = ""
    gate_api_base: str = ""
    moderation_model: str = ""
    moderation_api_base: str = ""

    # The workspace assistant's default model connection ("provider/model" + api_base),
    # used only when a workspace has no assistant yet and one is lazily created --
    # afterwards the assistant's own model profile (editable in the personas UI) is the
    # source of truth. Defaults match the dev stack's local Ollama.
    # Cold-start default for the workspace assistant's model profile. Deliberately
    # empty: a fresh install has no model provider yet, and baking in a specific local
    # model pointed every clean deployment at an ollama host and a model tag that were
    # not there. Set these (or edit the "Assistant model" profile in the UI) once a
    # provider exists -- e.g. "anthropic/claude-sonnet-5", or "ollama/<tag>" with
    # assistant_api_base pointing at the ollama host.
    assistant_model: str = ""
    assistant_api_base: str = ""

    # Where exec environments reach the hosted git store over smart-HTTP (routes/git_http).
    # Local sibling containers use the api's in-network name; k8s/cloud runners need a
    # routable URL. Replaces the old store-volume mount entirely.
    git_http_base: str = "http://pyrrhula_api_1:8000"
    # JSON list of exec-engine declarations (see core/exec_engines.py + docs/
    # exec-engines.md). Empty + legacy PYRRHULA_EXEC_SOCKET => one synthesized 'local'
    # socket engine (back-compat).
    exec_engines: str = ""
    # Where a *browser* reaches this deployment -- the base of a preview share link that
    # gets sent to a human tester. Distinct from git_http_base, which is an in-network
    # address (``pyrrhula_api_1``) meaningless outside the container network. Empty means
    # links are emitted relative, which still works when the reader is already on the UI.
    public_base_url: str = ""
    # How long a preview environment serves before the reaper stops it. Previews hold a
    # container for their whole life, so the default is a working session, not a week.
    preview_ttl_seconds: int = 4 * 3600
    preview_max_ttl_seconds: int = 24 * 3600
    # The image previews run. Needs a Python interpreter and nothing else: the serving
    # command is dependency-free stdlib (see core/previews/service.py).
    preview_image: str = "docker.io/library/python:3.12-slim"
    # Self-serve organization signup (POST /auth/signup). Disable on deployments where
    # only the platform admin creates tenants.
    allow_tenant_signup: bool = True
    # What a tenant that has not chosen gets for `POST /auth/register` -- joining an
    # EXISTING organization, which is a different question from creating a new one.
    # `closed` (nobody self-registers), `request` (an admin approves each applicant) or
    # `open`. Closed by default: the behaviour this replaced was ungated, so a
    # deployment that upgraded into a permissive default would gain the setting and keep
    # the hole. A tenant overrides it in `tenant.settings.registration_policy`.
    default_registration_policy: str = "closed"

    # Per-principal budget must absorb a normal SPA session: the session view alone
    # polls ~5 queries every 10s, and a second tab doubles that. 60/min starved real
    # browsers into 429 loops (observed live on the SSE stream).
    rate_limit_requests: int = 300
    rate_limit_tenant_requests: int = 1200
    rate_limit_window_seconds: int = 60

    # BlobStore is the local filesystem by default (A1.2). Setting blob_s3_bucket below
    # selects the S3-compatible adapter instead -- the swap happens in the composition
    # root (api/blob_store_factory.py), which is a config branch, exactly one level up
    # from these fields. This comment used to deny that branch existed.
    blob_store_root: str = "/app/data/blobs"
    # Workflow packs an operator supplies by hand, for deployments that cannot reach the
    # pinned plugin repositories (private, air-gapped, or simply offline). Every
    # subdirectory holding a plugin.json is registered and synced at boot. Mount a host
    # directory or a ConfigMap here; the platform only ever reads it.
    plugin_drop_dir: str = "/app/plugins-local"
    # S3-compatible blob storage (§13.8): a bucket selects the S3 adapter over the
    # local filesystem store. Credentials come from the ambient AWS chain, never here.
    blob_s3_bucket: str = ""
    blob_s3_endpoint: str = ""
    blob_s3_region: str = "us-east-1"
    blob_s3_prefix: str = ""

    # A1.3: "dimension pinned in config" -- embedding_dimension is the deployment's
    # declared truth; get_embedding_provider() asserts the selected adapter actually
    # produces vectors of this length at startup, so a config/adapter mismatch is a loud
    # startup error, not a silent zero-recall bug discovered at query time.
    embedding_model: str = "local/BAAI/bge-m3"
    embedding_dimension: int = 1024

    # A1.7: "config to disable reranking (degraded mode for tiny deployments)" --
    # reranker_enabled=False means core.knowledge.retrieval.assemble.search_and_budget
    # gets reranker=None, and ranking falls back to WRRF order untouched.
    reranker_enabled: bool = True
    reranker_model: str = "local/BAAI/bge-reranker-v2-m3"

    def model_post_init(self, __context: object) -> None:
        if not self.jwt_secret:
            # >=32 bytes: PyJWT warns below that for HS256 (RFC 7518 Sec3.2).
            object.__setattr__(self, "jwt_secret", "pyrrhula_jwt_dev_only_insecure_secret_key")
            warnings.warn(
                "PYRRHULA_JWT_SECRET not set; using an insecure development-only "
                "default. Set it explicitly outside local dev.",
                stacklevel=2,
            )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings

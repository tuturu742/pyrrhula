"""Process-wide settings, read once from the environment.

Two database URLs on purpose: ``database_url`` is the migration/admin connection (table
owner: the ``migrate`` entrypoint's DDL, plus the two things the api may only do as owner --
entity-schema DDL and NULL-tenant plugin rows); ``app_database_url`` is
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

    single_tenant_ui: bool = False
    # PYRRHULA_SINGLE_TENANT_UI pins requests to one tenant when no X-Pyrrhula-Tenant
    # header is present -- "the MVP UI exposes one tenant's worth of
    # functionality" is a feature flag over a multi-tenant core, not a
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

    # Bootstrap login for the reserved admin tenant (organization "admin" on the normal
    # login form): when both are set, api startup idempotently ensures this owner account
    # exists. Rotating the password here does NOT update an existing account.
    admin_email: str = ""
    admin_password: str = ""
    # SearXNG (or compatible) base URL registered as the workspace `web_search` MCP
    # server when a persona's search switch is first enabled. Empty = the preset's
    # unreachable placeholder (structure without egress).
    web_search_url: str = ""
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
    # The ceiling on how long a preview environment may serve before the reaper stops
    # it. The default lifetime is an organization preference (core.tenancy.preferences);
    # this is the bound the operator imposes on every organization, because a preview
    # holds a container for its whole life.
    preview_max_ttl_seconds: int = 24 * 3600
    # The image previews run. Needs a Python interpreter and nothing else: the serving
    # command is dependency-free stdlib (see core/previews/service.py).
    preview_image: str = "docker.io/library/python:3.12-slim"
    # What a fresh deployment starts with for self-serve organization signup
    # (POST /auth/signup). The admin console's switch (core.deployment_settings) wins
    # once set; this exists so an operator installing a closed instance has it closed
    # before the first boot rather than after the first click.
    allow_tenant_signup: bool = True
    # Per-principal budget must absorb a normal SPA session: the session view alone
    # polls ~5 queries every 10s, and a second tab doubles that. 60/min starved real
    # browsers into 429 loops (observed live on the SSE stream).
    rate_limit_requests: int = 300
    rate_limit_tenant_requests: int = 1200
    rate_limit_window_seconds: int = 60

    # BlobStore is the local filesystem by default. Setting blob_s3_bucket below
    # selects the S3-compatible adapter instead -- the swap happens in the composition
    # root (api/blob_store_factory.py), which is a config branch, exactly one level up
    # from these fields. This comment used to deny that branch existed.
    blob_store_root: str = "/app/data/blobs"
    # Workflow packs an operator supplies by hand, for deployments that cannot reach the
    # pinned plugin repositories (private, air-gapped, or simply offline). Every
    # subdirectory holding a plugin.json is registered and synced at boot. Mount a host
    # directory or a ConfigMap here; the platform only ever reads it.
    plugin_drop_dir: str = "/app/plugins-local"
    # S3-compatible blob storage: a bucket selects the S3 adapter over the
    # local filesystem store. Credentials come from the ambient AWS chain, never here.
    blob_s3_bucket: str = ""
    blob_s3_endpoint: str = ""
    blob_s3_region: str = "us-east-1"
    blob_s3_prefix: str = ""

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

"""FastAPI application entrypoint. Run via ``uvicorn api.main:app`` (docker/entrypoint.sh)."""

from __future__ import annotations

import asyncio
import importlib.metadata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from api.overseer import routes as overseer_routes
from api.redis_client import close_redis
from api.routes import (
    admin,
    agents,
    assist,
    auth,
    citations,
    entities,
    export,
    git_http,
    inference,
    knowledge,
    manifests,
    mcp,
    me,
    previews,
    process_definitions,
    reports,
    repos,
    resolutions,
    secrets,
    sessions,
    tenant_settings,
    tenant_users,
    usage,
    vocabulary,
    workflows,
    workspaces,
)
from core.observability.otel import configure_tracing
from core.tenancy.scope import dispose_engine
from core.usage_limits import UsageLimitExceededError
from core.version import APP_VERSION

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_tracing(service_name="pyrrhula-api")
    # First boot on a fresh database: the default (baked) workflow plugin syncs itself,
    # the env-configured platform-admin account is ensured, and the admin console's
    # retrieval override is folded in. All idempotent, and all retried until the database
    # answers (core.startup): on a fresh install this process routinely starts before
    # Postgres does, and a one-shot attempt left deployments with no admin account.
    from api.admin_bootstrap import ensure_admin_account
    from core.deployment_settings import apply_retrieval_override
    from core.plugins.service import ensure_default_synced
    from core.startup import run_boot_hooks

    async def _retrieval_override() -> None:
        applied = await apply_retrieval_override()
        if applied:
            log.info("retrieval.override_applied", model=applied.get("embedding_model"))

    # Behind the app, not in front of it. These retry until the database answers, which
    # is what a fresh install needs -- the api routinely starts before Postgres does, and
    # a one-shot attempt left deployments with no admin account. Awaited here, that same
    # patience became the startup path: with no database reachable at all, each hook sat
    # out its own deadline before the app served its first request, so the process took
    # ten minutes to come up and a readiness probe failed for all of it. Serving
    # immediately and bootstrapping behind it is strictly better -- the hooks still run,
    # still retry, and still finish long before anyone needs what they write.
    boot = asyncio.create_task(
        run_boot_hooks(
            [
                ("plugins.default_sync", ensure_default_synced),
                ("admin_bootstrap", ensure_admin_account),
                ("retrieval.override", _retrieval_override),
            ]
        )
    )
    log.info("api.startup")
    try:
        yield
    finally:
        # A shutdown mid-bootstrap is a shutdown, not a reason to wait out three
        # deadlines: the hooks are idempotent and the next start runs them again.
        boot.cancel()
        with suppress(asyncio.CancelledError):
            await boot
    await close_redis()
    # Symmetrical with Redis, and for the same reason: a shutdown that leaves its pool
    # open leaves Postgres connections for the server to reap. It is invisible in
    # production, where the process exits and takes them with it, and cumulative under
    # test -- every `TestClient(app)` context builds the engine on its own internal loop
    # and abandoned it here, so an API suite marched a hundred pools towards Postgres's
    # connection limit and failed whichever test happened to be running when it arrived.
    await dispose_engine()
    log.info("api.shutdown")


app = FastAPI(title="Pyrrhula API", version=APP_VERSION, lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app)


class StripApiPrefixMiddleware:
    """Tolerate an optional leading ``/api`` on every route.

    The web UI calls ``/api/<path>`` and relies on a proxy to strip the prefix. Local
    proxies (the web container's nginx, k8s ingress) can rewrite paths; AWS ALBs
    cannot -- and routing ``/api/*`` straight to the api at the load balancer is what
    keeps the UI working across api redeploys (a startup-resolved upstream IP goes
    stale when Fargate replaces the task). No real route starts with ``/api``, so the
    strip is unambiguous."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path", "").startswith("/api/"):
            scope = dict(scope)
            scope["path"] = scope["path"][4:]
            scope["raw_path"] = scope["path"].encode()
        await self.app(scope, receive, send)


app.add_middleware(StripApiPrefixMiddleware)


@app.exception_handler(UsageLimitExceededError)
async def _usage_limit_handler(request: Request, exc: UsageLimitExceededError) -> JSONResponse:
    # A hit cap is a client-visible state ("try again after midnight UTC"), never a 500.
    return JSONResponse(status_code=429, content={"detail": str(exc)})


# health and auth are the only routes reachable without a resolved RequestContext
# (test_route_inventory.py enforces this).
app.include_router(auth.router)
app.include_router(me.router)
app.include_router(tenant_users.router)
app.include_router(tenant_settings.router)
app.include_router(sessions.router)
app.include_router(sessions.stream_router)
app.include_router(workspaces.router)
app.include_router(knowledge.router)
app.include_router(process_definitions.router)
app.include_router(usage.router)
app.include_router(citations.router)
app.include_router(resolutions.router)
app.include_router(manifests.router)
app.include_router(agents.router)
app.include_router(assist.router)
app.include_router(vocabulary.router)
app.include_router(workflows.router)
app.include_router(repos.router)
app.include_router(previews.router)
# The preview share link: deliberately unauthenticated (it is meant to be sent to a
# tester who has no account). Its authorization is the signed token in the path, which
# carries the tenant id -- so the handler still reads under a normal tenant_scope.
app.include_router(previews.public_router)
app.include_router(git_http.router)
# Same shape as git_http above: its own bearer token, no session JWT -- a harness running
# in a container cannot hold a session.
app.include_router(inference.router)
app.include_router(secrets.router)
app.include_router(entities.router)
app.include_router(export.router)
app.include_router(reports.router)
app.include_router(mcp.router)
app.include_router(overseer_routes.router)
# Platform admin, gated by require_platform_admin (ops token OR admin-tenant JWT) --
# not by get_request_context alone like the tenant routes above.
app.include_router(admin.router)


# Read once at import: the answer cannot change while the process runs, and a probe
# that is hit every few seconds should not pay for a metadata lookup each time.
# "unknown" is for a source checkout that was never installed -- a released image always
# has the distribution metadata, because the image installs the wheel.
try:
    VERSION = importlib.metadata.version("pyrrhula")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover - source checkout
    VERSION = "unknown"


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness/readiness probe. No DB round-trip on purpose — this is the process check,
    not a dependency check. It carries the version because an operator running a pulled
    image has no other way to tell which build answered."""
    return {"status": "ok", "version": VERSION}

"""FastAPI application entrypoint. Run via ``uvicorn api.main:app`` (docker/entrypoint.sh)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
    # First boot on a fresh database: the default (baked) workflow plugin syncs itself
    # and the env-configured platform-admin account is ensured. Both best-effort and
    # idempotent -- a failure surfaces in logs/console panels, never blocks startup.
    try:
        from core.plugins.service import ensure_default_synced

        await ensure_default_synced()
    except Exception as exc:  # noqa: BLE001
        log.warning("plugins.default_sync_failed", error=str(exc)[:300])
    try:
        from api.admin_bootstrap import ensure_admin_account

        await ensure_admin_account()
    except Exception as exc:  # noqa: BLE001
        log.warning("admin_bootstrap.failed", error=str(exc)[:300])
    try:
        from core.deployment_settings import apply_retrieval_override_to_settings

        applied = await apply_retrieval_override_to_settings()
        if applied:
            log.info("retrieval.override_applied", model=applied.get("embedding_model"))
    except Exception as exc:  # noqa: BLE001
        log.warning("retrieval.override_failed", error=str(exc)[:300])
    log.info("api.startup")
    yield
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
app.include_router(secrets.router)
app.include_router(entities.router)
app.include_router(export.router)
app.include_router(reports.router)
app.include_router(mcp.router)
app.include_router(overseer_routes.router)
# Platform admin, gated by require_platform_admin (ops token OR admin-tenant JWT) --
# not by get_request_context alone like the tenant routes above.
app.include_router(admin.router)


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness/readiness probe. No DB round-trip on purpose — this is the process check,
    not a dependency check. Real dependency wiring lands with T0.2/T0.3."""
    return {"status": "ok"}

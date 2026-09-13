"""Preview environments: start one from a repo's latest build, list, stop -- and serve it.

Two routers with deliberately different auth:

``router`` is the ordinary tenant-scoped management surface. ``public_router`` is the
share link: no session, no tenant header, because the whole point is to send it to a
human who does not have a Pyrrhula account. Its authorization is the signed token in the
path, which carries the tenant id -- so the handler still opens a normal
``tenant_scope`` and RLS applies exactly as everywhere else. See core/previews/tokens.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.responses import RedirectResponse, Response, StreamingResponse

from api.authz import require_tenant_permission
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import (
    RateLimitExceededError,
    check_rate_limit,
    rate_limit_by_principal,
    rate_limit_by_tenant,
)
from api.play_headers import PLAY_HEADERS, content_type_for
from core.config import get_settings
from core.previews.models import PreviewEnvironmentRow
from core.previews.service import (
    create_preview,
    get_preview,
    is_serveable,
    list_previews,
    preview_name,
    preview_share_url,
    set_status,
)
from core.previews.tokens import mint_preview_token, read_preview_token
from core.tenancy.context import RequestContext

log = structlog.get_logger()

router = APIRouter(
    prefix="/previews",
    tags=["previews"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)

# Mounted at /p; reached in a browser as /api/p/... (StripApiPrefixMiddleware), which is
# the only path routed to the API on compose, k8s and ECS alike.
public_router = APIRouter(prefix="/p", tags=["previews-public"])


class PreviewResponse(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    engine_key: str | None = None
    image: str = ""
    repo_id: uuid.UUID | None = None
    workspace_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    artifact_name: str = ""
    created_by_label: str = ""
    last_error: str = ""
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    url: str = ""


class CreatePreviewRequest(BaseModel):
    repo_id: uuid.UUID
    workspace_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    ttl_seconds: int | None = None


@router.get("")
async def list_previews_endpoint(
    workspace_id: uuid.UUID | None = None,
    repo_id: uuid.UUID | None = None,
    include_finished: bool = False,
    ctx: RequestContext = Depends(get_request_context),
) -> list[PreviewResponse]:
    rows = await list_previews(
        ctx.tenant_id,
        workspace_id=workspace_id,
        repo_id=repo_id,
        include_finished=include_finished,
    )
    return [PreviewResponse(**row) for row in rows]


@router.post("", status_code=202)
async def create_preview_endpoint(
    body: CreatePreviewRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> PreviewResponse:
    """Deploy the repo's latest build artifact to a running container.

    The row is claimed here and the container is started by the worker: the API process
    holds no engine access (no socket, no cluster token, no boto session)."""
    from api.job_queue_factory import get_job_queue
    from core.audit.service import AuditService
    from core.repos.service import get_repo, store_key

    await require_tenant_permission(ctx, "repo:manage")
    settings = get_settings()

    repo = await get_repo(ctx.tenant_id, body.repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="no such repo")
    if not repo.artifact_name:
        raise HTTPException(status_code=409, detail="repo has no build artifact configured")
    if not repo.artifact_name.endswith((".tar.gz", ".tgz")):
        raise HTTPException(
            status_code=409, detail="preview serving expects a .tar.gz artifact (a web build)"
        )

    ttl = min(
        int(body.ttl_seconds or settings.preview_ttl_seconds),
        settings.preview_max_ttl_seconds,
    )
    from core.exec_engines import get_tenant_engine_key

    engine_key = await get_tenant_engine_key(ctx.tenant_id)
    name = preview_name(body.repo_id)
    preview_id = await create_preview(
        ctx.tenant_id,
        name=name,
        repo_id=body.repo_id,
        workspace_id=body.workspace_id,
        session_id=body.session_id,
        artifact_name=repo.artifact_name,
        engine_key=engine_key,
        image=settings.preview_image,
        ttl_seconds=ttl,
        created_by_principal_id=ctx.principal_id,
    )
    # The share token outlives nothing: it expires with the preview it names.
    share_token = mint_preview_token(ctx.tenant_id, preview_id, ttl_seconds=ttl)

    # Audited on create and stop only -- never per proxied request, which would turn a
    # game's asset loading into a write storm against a hash-chained table.
    await AuditService().append(
        tenant_id=ctx.tenant_id,
        actor_principal_id=ctx.principal_id,
        action="preview.create",
        resource_type="preview_environment",
        resource_id=preview_id,
        query={"repo_id": str(body.repo_id), "artifact": repo.artifact_name, "ttl": ttl},
    )
    await get_job_queue().enqueue(
        ctx.tenant_id,
        "start_preview",
        {
            "tenant_id": str(ctx.tenant_id),
            "preview_id": str(preview_id),
            "store_key": store_key(ctx.tenant_id, repo.key),
            "share_token": share_token,
            "ttl_seconds": ttl,
        },
    )
    row = await get_preview(ctx.tenant_id, preview_id)
    assert row is not None
    return PreviewResponse(
        id=row.id,
        name=row.name,
        status=row.status,
        engine_key=row.engine_key,
        image=row.image,
        repo_id=row.repo_id,
        workspace_id=row.workspace_id,
        session_id=row.session_id,
        artifact_name=row.artifact_name,
        created_by_label=row.created_by_label,
        last_error=row.last_error,
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        url=preview_share_url(share_token),
    )


class ShareRequest(BaseModel):
    # Push the preview's deadline out so a link handed over right now is actually usable
    # for a while. False mints a token that expires with the preview as it stands.
    extend: bool = True
    ttl_seconds: int | None = None


@router.post("/{preview_id}/share")
async def share_preview_endpoint(
    preview_id: uuid.UUID,
    body: ShareRequest | None = None,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, str]:
    """Mint a fresh share link for a running preview.

    Share tokens expire, and the URL is deliberately not stored anywhere -- it is
    derived on demand from the row, so "my link stopped working" is answered by asking
    for another one rather than by redeploying and losing the container. Extending the
    deadline at the same time is the point: a token minted against a preview with four
    minutes left would be a link that dies while the recipient is still opening it."""
    from core.previews.service import extend_expiry

    body = body or ShareRequest()
    settings = get_settings()
    row = await get_preview(ctx.tenant_id, preview_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such preview")
    if row.status != "running":
        # A token cannot resurrect a container the reaper already stopped; say so plainly
        # rather than handing back a link that 404s.
        raise HTTPException(
            status_code=409,
            detail=f"preview is {row.status}; deploy it again to get a working link",
        )

    ttl = min(
        int(body.ttl_seconds or settings.preview_ttl_seconds),
        settings.preview_max_ttl_seconds,
    )
    if body.extend:
        expires_at = await extend_expiry(ctx.tenant_id, preview_id, ttl_seconds=ttl)
    else:
        expires_at = row.expires_at
        remaining = int((expires_at - datetime.now(UTC)).total_seconds()) if expires_at else 0
        if remaining <= 0:
            raise HTTPException(status_code=409, detail="preview has expired")
        ttl = min(ttl, remaining)

    token = mint_preview_token(ctx.tenant_id, preview_id, ttl_seconds=ttl)
    return {
        "url": preview_share_url(token),
        "expires_at": expires_at.isoformat() if expires_at else "",
    }


@router.delete("/{preview_id}", status_code=202)
async def stop_preview_endpoint(
    preview_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> dict[str, str]:
    from api.job_queue_factory import get_job_queue
    from core.audit.service import AuditService

    await require_tenant_permission(ctx, "repo:manage")
    row = await get_preview(ctx.tenant_id, preview_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such preview")
    await set_status(ctx.tenant_id, preview_id, "stopped")
    await AuditService().append(
        tenant_id=ctx.tenant_id,
        actor_principal_id=ctx.principal_id,
        action="preview.stop",
        resource_type="preview_environment",
        resource_id=preview_id,
        query={"name": row.name},
    )
    await get_job_queue().enqueue(
        ctx.tenant_id,
        "stop_preview",
        {
            "tenant_id": str(ctx.tenant_id),
            "preview_id": str(preview_id),
            "status": "stopped",
        },
    )
    return {"preview_id": str(preview_id), "status": "stopping"}


# ── the public share link ────────────────────────────────────────────────────────────
# Generous on purpose: a web game pulls dozens of files on load, so a per-IP budget sized
# for API calls would break the very thing being shared. Keyed on (ip, token) so one
# noisy preview cannot exhaust another's allowance.
_PREVIEW_RATE_LIMIT = 600
_PROXY_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
# Hop-by-hop headers must not be forwarded; Content-Length is recomputed by Starlette.
_DROP_HEADERS = {
    "transfer-encoding",
    "connection",
    "keep-alive",
    "content-length",
    "content-encoding",
    "server",
    "date",
}


async def _resolve(token: str) -> tuple[uuid.UUID, PreviewEnvironmentRow]:
    """Token -> (tenant_id, row). The token names its own tenant, so this opens a normal
    tenant-scoped read; an invalid token and a dead preview both 404, so someone guessing
    links learns nothing."""
    claims = read_preview_token(token)
    if claims is None:
        raise HTTPException(status_code=404, detail="no such preview")
    tenant_id, preview_id = claims
    row = await get_preview(tenant_id, preview_id)
    if row is None or not is_serveable(row):
        raise HTTPException(status_code=404, detail="this preview is no longer running")
    return tenant_id, row


@public_router.get("/{token}")
async def preview_index(token: str) -> RedirectResponse:
    await _resolve(token)
    return RedirectResponse(url=f"/api/p/{token}/index.html")


@public_router.get("/{token}/{path:path}")
async def preview_file(token: str, path: str, request: Request) -> Response:
    client_ip = request.client.host if request.client else "unknown"
    try:
        await check_rate_limit(f"preview:{client_ip}:{token[-16:]}", limit=_PREVIEW_RATE_LIMIT)
    except RateLimitExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc

    _, row = await _resolve(token)
    if not path:
        return RedirectResponse(url=f"/api/p/{token}/index.html")

    target = f"{row.internal_url.rstrip('/')}/{path.lstrip('/')}"
    client = httpx.AsyncClient(timeout=_PROXY_TIMEOUT)
    try:
        # Streamed, not buffered: a web build's .wasm/.pck routinely runs to tens of MB
        # and the artifact cap is 200 MiB -- holding that per request per player in the
        # api process is not acceptable.
        upstream = await client.send(client.build_request("GET", target), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        log.warning("preview.proxy_failed", name=row.name, error=str(exc))
        raise HTTPException(status_code=502, detail="preview is not responding") from exc

    if upstream.status_code >= 400:
        status = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=status, detail="no such file in the preview")

    async def _close() -> None:
        await upstream.aclose()
        await client.aclose()

    headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_HEADERS}
    # Content type from the path, and the isolation headers stamped rather than
    # forwarded -- the container's static server is not trusted to get either right,
    # and COOP/COEP are what make SharedArrayBuffer (hence Godot 4) work at all.
    headers.pop("content-type", None)
    headers.update(PLAY_HEADERS)
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        media_type=content_type_for(path),
        headers=headers,
        background=BackgroundTask(_close),
    )

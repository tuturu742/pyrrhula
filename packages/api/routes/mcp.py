"""MCP registry admin.

The registry is workspace configuration, and configuration is exactly where the allowlist
lives -- so this is the surface that decides what agents in a workspace can reach. It is
kept deliberately small: register/list/delete a server and its enabled tools. There is no
"call a tool" endpoint, because tool calls belong to a turn and a turn belongs to the
interpreter; an HTTP path that invoked a tool outside a session would be a path with no
phase policy to check against.

`credential_ref` is a pointer into a secret manager. The registry refuses a value that
looks like a live key outright (`core.mcp.registry`), so the mistake fails at the door
rather than in a backup six months later.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select

from api.authz import require_tenant_permission
from api.mcp_transport_factory import get_mcp_transport
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from core.mcp.registry import (
    WEB_SEARCH_PRESET,
    CredentialInRegistryError,
    McpServerRow,
    get_server,
    list_servers,
    register_server,
)
from core.ports.mcp import McpTransportError
from core.tenancy.context import RequestContext
from core.tenancy.models import WorkspaceMembership
from core.tenancy.scope import tenant_scope

router = APIRouter(
    prefix="/mcp-servers",
    tags=["mcp"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


class McpServerRequest(BaseModel):
    workspace_id: uuid.UUID
    key: str
    url: str
    enabled_tools: list[str]
    effectful_tools: list[str] = []
    credential_ref: str | None = None
    require_confirmation: bool = True
    # Per-session ceiling on calls to this server; None = unlimited. The cap lives here
    # because an external server is never told which session is calling it.
    max_calls_per_session: int | None = None
    # Both default to the platform's value when omitted; a server that answers instantly
    # should say so rather than inherit a build tool's ten-minute patience.
    timeout_seconds: int | None = None
    max_result_chars: int | None = None
    # Transport-specific knobs for this server, e.g. {"engines": "bing,duckduckgo"}.
    options: dict[str, object] = {}


class McpServerResponse(BaseModel):
    id: uuid.UUID
    key: str
    url: str
    enabled_tools: list[str]
    effectful_tools: list[str]
    require_confirmation: bool
    max_calls_per_session: int | None = None
    timeout_seconds: int | None = None
    max_result_chars: int | None = None
    options: dict[str, object] = {}
    credential_ref: str | None


def _to_response(row: object) -> McpServerResponse:
    return McpServerResponse(
        id=row.id,  # type: ignore[attr-defined]
        key=row.key,  # type: ignore[attr-defined]
        url=row.url,  # type: ignore[attr-defined]
        enabled_tools=list(row.enabled_tools),  # type: ignore[attr-defined]
        effectful_tools=list(row.effectful_tools),  # type: ignore[attr-defined]
        require_confirmation=row.require_confirmation,  # type: ignore[attr-defined]
        max_calls_per_session=row.max_calls_per_session,  # type: ignore[attr-defined]
        timeout_seconds=row.timeout_seconds,  # type: ignore[attr-defined]
        max_result_chars=row.max_result_chars,  # type: ignore[attr-defined]
        options=dict(row.options or {}),  # type: ignore[attr-defined]
        credential_ref=row.credential_ref,  # type: ignore[attr-defined]
    )


@router.get("")
async def list_mcp_servers(
    workspace_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> list[McpServerResponse]:
    return [_to_response(row) for row in await list_servers(ctx.tenant_id, workspace_id)]


async def _require_workspace_member(ctx: RequestContext, workspace_id: uuid.UUID) -> None:
    async with tenant_scope(ctx.tenant_id) as session:
        member = await session.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.principal_id == ctx.principal_id,
            )
        )
    if member is None:
        raise HTTPException(status_code=403, detail="not a member of this workspace")


@router.put("", status_code=200)
async def upsert_mcp_server(
    body: McpServerRequest, ctx: RequestContext = Depends(get_request_context)
) -> McpServerResponse:
    """PUT rather than POST: registering the same server twice with the same configuration
    is the same configuration, and a deployment's setup script should be re-runnable
    without accumulating duplicates.

    This WIDENS a workspace's tool allowlist, so it is gated like workflow selection
    (``workflow:manage`` -- owner/admin) plus membership in the target workspace."""
    await require_tenant_permission(ctx, "workflow:manage")
    await _require_workspace_member(ctx, body.workspace_id)
    try:
        row = await register_server(
            ctx.tenant_id,
            body.workspace_id,
            body.key,
            body.url,
            enabled_tools=body.enabled_tools,
            effectful_tools=body.effectful_tools,
            credential_ref=body.credential_ref,
            require_confirmation=body.require_confirmation,
            max_calls_per_session=body.max_calls_per_session,
            timeout_seconds=body.timeout_seconds,
            max_result_chars=body.max_result_chars,
            options=body.options,
        )
    except CredentialInRegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _to_response(row)


class PresetResponse(BaseModel):
    key: str
    url: str
    enabled_tools: list[str]
    effectful_tools: list[str]
    require_confirmation: bool


@router.get("/presets")
async def list_presets() -> list[PresetResponse]:
    """The web-search preset: a registry entry a deployment enables, not a special code
    path. "Web search is just an MCP server behind a workspace policy flag" is only true if
    it is registered the same way everything else is."""
    return [PresetResponse(**WEB_SEARCH_PRESET)]  # type: ignore[arg-type]


@router.get("/{key}")
async def read_mcp_server(
    key: str, workspace_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> McpServerResponse:
    row = await get_server(ctx.tenant_id, workspace_id, key)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no MCP server {key!r} in this workspace")
    return _to_response(row)


class McpServerTestResponse(BaseModel):
    ok: bool
    detail: str
    # What the server offered, so a typo in the allowlist is visible next to the real names.
    tools: list[str]


@router.post("/{key}/test")
async def test_mcp_server(
    key: str, workspace_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> McpServerTestResponse:
    """Ask the server what it offers, the same discovery a turn runs, and say so plainly.

    A turn treats an unreachable server as "no tools this turn" and carries on, which is
    right for a session and wrong for the person who just typed the URL: they learn about
    a bad address from a persona explaining why it cannot use its tool. This is the same
    listing, reported to the human instead of swallowed. Read-only, so membership is enough.
    """
    await _require_workspace_member(ctx, workspace_id)
    row = await get_server(ctx.tenant_id, workspace_id, key)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no MCP server {key!r} in this workspace")
    try:
        specs = await get_mcp_transport().list_tools(row.to_ref())
    except McpTransportError as exc:
        return McpServerTestResponse(ok=False, detail=str(exc)[:500], tools=[])
    offered = [s.name for s in specs]
    missing = [name for name in row.enabled_tools if name not in offered]
    if missing:
        return McpServerTestResponse(
            ok=False,
            detail=f"reachable, but not offering {', '.join(missing)} -- check the tool names",
            tools=offered,
        )
    return McpServerTestResponse(
        ok=True, detail=f"reachable, offers {len(offered)} tool(s)", tools=offered
    )


@router.delete("/{key}", status_code=204)
async def delete_mcp_server(
    key: str, workspace_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> None:
    """The delete the module docstring always promised. Same gate as the upsert."""
    await require_tenant_permission(ctx, "workflow:manage")
    await _require_workspace_member(ctx, workspace_id)
    async with tenant_scope(ctx.tenant_id) as session:
        row = await session.scalar(
            select(McpServerRow).where(
                McpServerRow.workspace_id == workspace_id, McpServerRow.key == key
            )
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"no MCP server {key!r} in this workspace")
        await session.delete(row)


# ── mcp asset window (images generated by registered servers, shown to humans) ───────
# A registered server (e.g. an image-generation sidecar) returns asset URLs on ITS OWN
# host -- unreachable from a browser outside the cluster. This route is the bounded
# window: only paths under a server the workspace has REGISTERED are fetched, so the
# reachable surface is exactly the operator-approved endpoints, nothing else (no open
# proxy). Membership in the workspace is required, same as reading its transcript.
_ASSET_MAX_BYTES = 20 * 1024 * 1024
_ASSET_TIMEOUT_S = 15.0


@router.get("/asset/{workspace_id}/{key}/{path:path}")
async def fetch_mcp_asset(
    workspace_id: uuid.UUID,
    key: str,
    path: str,
    ctx: RequestContext = Depends(get_request_context),
) -> Response:
    from urllib.parse import urlsplit

    import httpx
    from fastapi.responses import Response

    await _require_workspace_member(ctx, workspace_id)
    row = await get_server(ctx.tenant_id, workspace_id, key)
    if row is None or not row.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=404, detail=f"no remote MCP server {key!r} here")
    parts = urlsplit(row.url)
    target = f"{parts.scheme}://{parts.netloc}/{path}"
    try:
        async with httpx.AsyncClient(timeout=_ASSET_TIMEOUT_S) as client:
            upstream = await client.get(target)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"asset fetch failed: {exc}") from exc
    if upstream.status_code != 200:
        raise HTTPException(status_code=upstream.status_code, detail="asset not available")
    if len(upstream.content) > _ASSET_MAX_BYTES:
        raise HTTPException(status_code=413, detail="asset too large")
    return Response(
        content=upstream.content,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        headers={"Cache-Control": "private, max-age=3600"},
    )

"""vocabulary overlay listing/resolution/switching. The frontend fetches one
resolved ``{key, labels}`` per workspace (the fallback-chain logic lives in
``core.vocabulary.service``, not duplicated client-side) and switches it live via the
workspace/tenant-default PATCH endpoints below.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.authz import require_permission, require_tenant_permission
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from core.tenancy.context import RequestContext
from core.vocabulary.models import VocabularyOverlayRow
from core.vocabulary.service import (
    list_overlays,
    resolve_overlay_for_tenant,
    resolve_overlay_for_workspace,
    set_tenant_default_overlay,
    set_workspace_overlay,
)

router = APIRouter(
    tags=["vocabulary"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


class VocabularyOverlayResponse(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    labels: dict[str, str]


def _overlay_response(row: VocabularyOverlayRow) -> VocabularyOverlayResponse:
    return VocabularyOverlayResponse(id=row.id, key=row.key, name=row.name, labels=row.labels)


@router.get("/vocabulary-overlays")
async def list_vocabulary_overlays(
    ctx: RequestContext = Depends(get_request_context),
) -> list[VocabularyOverlayResponse]:
    overlays = await list_overlays(ctx.tenant_id)
    return [_overlay_response(o) for o in overlays]


@router.get("/workspaces/{workspace_id}/vocabulary-overlay")
async def get_workspace_vocabulary_overlay(
    workspace_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> VocabularyOverlayResponse:
    overlay = await resolve_overlay_for_workspace(ctx.tenant_id, workspace_id)
    if overlay is None:
        raise HTTPException(
            status_code=404, detail=f"no resolvable vocabulary overlay for workspace {workspace_id}"
        )
    return _overlay_response(overlay)


@router.get("/tenant/vocabulary-overlay")
async def get_tenant_vocabulary_overlay(
    ctx: RequestContext = Depends(get_request_context),
) -> VocabularyOverlayResponse:
    """The overlay for pages with no workspace in scope -- the tenant default, else the
    system default. Without it the shell labelled a software-development organization's
    home page "World / Campaigns" until the user opened a workspace."""
    overlay = await resolve_overlay_for_tenant(ctx.tenant_id)
    if overlay is None:
        raise HTTPException(status_code=404, detail="no resolvable vocabulary overlay")
    return _overlay_response(overlay)


class SetWorkspaceOverlayRequest(BaseModel):
    overlay_id: uuid.UUID | None


@router.patch("/workspaces/{workspace_id}/vocabulary-overlay")
async def set_workspace_vocabulary_overlay(
    workspace_id: uuid.UUID,
    body: SetWorkspaceOverlayRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> VocabularyOverlayResponse:
    await require_permission(ctx, "manage_workspace", "workspace", workspace_id)
    try:
        await set_workspace_overlay(ctx.tenant_id, workspace_id, body.overlay_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    overlay = await resolve_overlay_for_workspace(ctx.tenant_id, workspace_id)
    if overlay is None:
        raise HTTPException(
            status_code=404, detail=f"no resolvable vocabulary overlay for workspace {workspace_id}"
        )
    return _overlay_response(overlay)


class SetTenantDefaultOverlayRequest(BaseModel):
    overlay_key: str | None


@router.patch("/tenant/vocabulary-overlay")
async def set_tenant_default_vocabulary_overlay(
    body: SetTenantDefaultOverlayRequest, ctx: RequestContext = Depends(get_request_context)
) -> dict[str, str | None]:
    await require_tenant_permission(ctx, "manage_tenant")
    await set_tenant_default_overlay(ctx.tenant_id, body.overlay_key)
    return {"default_vocabulary_overlay_key": body.overlay_key}

"""Owner-facing organization preferences: login lifetime, default preview lifetime,
and whether retrieval uses the deployment's reranker.

Each of these was an environment variable once. They are choices two organizations
reasonably make differently, so they live on the tenant (``core.tenancy.preferences``)
and are edited here by anyone holding ``manage_tenant``. Every change is audited.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.authz import require_tenant_permission
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from core.audit.service import AuditService
from core.tenancy.context import RequestContext
from core.tenancy.preferences import (
    DEFAULT_PREVIEW_TTL_SECONDS,
    DEFAULT_SESSION_LIFETIME_SECONDS,
    MAX_SESSION_LIFETIME_SECONDS,
    MIN_PREVIEW_TTL_SECONDS,
    MIN_SESSION_LIFETIME_SECONDS,
    TenantPreferences,
    get_preferences,
    preview_ttl_ceiling,
    set_preferences,
)

router = APIRouter(
    prefix="/tenant/settings",
    tags=["tenant-settings"],
    dependencies=[Depends(rate_limit_by_principal), Depends(rate_limit_by_tenant)],
)


class TenantSettingsBody(BaseModel):
    """Only the fields sent are changed; an omitted field keeps its value."""

    session_lifetime_seconds: int | None = None
    preview_ttl_seconds: int | None = None
    reranker_enabled: bool | None = None


class TenantSettingsBounds(BaseModel):
    session_lifetime_min: int = MIN_SESSION_LIFETIME_SECONDS
    session_lifetime_max: int = MAX_SESSION_LIFETIME_SECONDS
    session_lifetime_default: int = DEFAULT_SESSION_LIFETIME_SECONDS
    preview_ttl_min: int = MIN_PREVIEW_TTL_SECONDS
    # The operator's ceiling (PYRRHULA_PREVIEW_MAX_TTL_SECONDS): the one bound here that
    # is a deployment fact rather than a constant.
    preview_ttl_max: int
    preview_ttl_default: int = DEFAULT_PREVIEW_TTL_SECONDS
    # Whether the deployment has a reranker at all (Admin -> Models); the organization's
    # switch means nothing without one, and the page should say so.
    reranker_available: bool


class TenantSettingsResponse(BaseModel):
    session_lifetime_seconds: int
    preview_ttl_seconds: int
    reranker_enabled: bool
    bounds: TenantSettingsBounds


def _response(prefs: TenantPreferences) -> TenantSettingsResponse:
    from core.deployment_settings import current_retrieval_models

    return TenantSettingsResponse(
        session_lifetime_seconds=prefs.session_lifetime_seconds,
        preview_ttl_seconds=prefs.preview_ttl_seconds,
        reranker_enabled=prefs.reranker_enabled,
        bounds=TenantSettingsBounds(
            preview_ttl_max=preview_ttl_ceiling(),
            reranker_available=current_retrieval_models().reranker_enabled,
        ),
    )


@router.get("")
async def get_tenant_settings(
    ctx: RequestContext = Depends(get_request_context),
) -> TenantSettingsResponse:
    """The organization's preferences with defaults filled in. Readable by any member:
    the values are not secrets, and the session view needs to know its own lifetime."""
    return _response(await get_preferences(ctx.tenant_id))


@router.put("")
async def put_tenant_settings(
    body: TenantSettingsBody,
    ctx: RequestContext = Depends(get_request_context),
) -> TenantSettingsResponse:
    await require_tenant_permission(ctx, "manage_tenant")
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(status_code=422, detail="nothing to change")
    try:
        prefs = await set_preferences(ctx.tenant_id, changes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await AuditService().append(
        tenant_id=ctx.tenant_id,
        actor_principal_id=ctx.principal_id,
        action="tenant.settings:update",
        resource_type="tenant",
        resource_id=ctx.tenant_id,
        query=changes,
    )
    return _response(prefs)

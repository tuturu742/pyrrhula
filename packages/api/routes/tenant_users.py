"""Owner-facing user management for the caller's OWN organization.

The platform-admin console could always create users in any tenant; an organization
owner could not add a teammate to their own — the largest multi-user hole the
pre-launch audit found. Same provisioning helpers as the admin console
(`create_tenant_user` + identity attach with orphan cleanup), gated on the caller
holding `manage_tenant` in the tenant they are acting on. Every change is audited.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

from adapters.identity.local.argon2_provider import LocalArgon2IdentityProvider
from api.authz import require_tenant_permission
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from core.audit.service import AuditService
from core.tenancy.context import RequestContext
from core.tenancy.provisioning import (
    create_tenant_user,
    delete_principal,
    list_tenant_users,
    set_principal_disabled,
)

router = APIRouter(
    prefix="/tenant/users",
    tags=["tenant-users"],
    dependencies=[Depends(rate_limit_by_principal), Depends(rate_limit_by_tenant)],
)

_ROLES = {"owner", "admin", "editor", "participant", "viewer"}
_identity_provider = LocalArgon2IdentityProvider()


class TenantUserOut(BaseModel):
    principal_id: uuid.UUID
    display_name: str
    role: str
    email: str | None
    disabled: bool


class CreateTenantUserRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str
    role: str = "participant"


@router.get("")
async def list_users(
    ctx: RequestContext = Depends(get_request_context),
) -> list[TenantUserOut]:
    await require_tenant_permission(ctx, "manage_tenant")
    return [
        TenantUserOut(
            principal_id=u.principal_id,
            display_name=u.display_name,
            role=u.role,
            email=u.email,
            disabled=u.disabled_at is not None,
        )
        for u in await list_tenant_users(ctx.tenant_id)
    ]


@router.post("", status_code=201)
async def create_user(
    body: CreateTenantUserRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, str]:
    await require_tenant_permission(ctx, "manage_tenant")
    if body.role not in _ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {sorted(_ROLES)}")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="password must be at least 8 characters")
    principal_id = await create_tenant_user(ctx.tenant_id, body.display_name, body.role)
    try:
        await _identity_provider.register_local(
            ctx.tenant_id, principal_id, str(body.email), body.password
        )
    except Exception as exc:  # noqa: BLE001 -- unique-email race: clean up the orphan
        await delete_principal(ctx.tenant_id, principal_id)
        raise HTTPException(status_code=409, detail="email already registered") from exc
    await AuditService().append(
        tenant_id=ctx.tenant_id,
        actor_principal_id=ctx.principal_id,
        action="user:create",
        resource_type="principal",
        resource_id=principal_id,
    )
    return {"principal_id": str(principal_id)}


@router.post("/{principal_id}/deactivate")
async def deactivate_user(
    principal_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, bool]:
    await require_tenant_permission(ctx, "manage_tenant")
    if principal_id == ctx.principal_id:
        raise HTTPException(status_code=409, detail="you cannot deactivate yourself")
    try:
        await set_principal_disabled(ctx.tenant_id, principal_id, True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await AuditService().append(
        tenant_id=ctx.tenant_id,
        actor_principal_id=ctx.principal_id,
        action="user:deactivate",
        resource_type="principal",
        resource_id=principal_id,
    )
    return {"disabled": True}


@router.post("/{principal_id}/reactivate")
async def reactivate_user(
    principal_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, bool]:
    await require_tenant_permission(ctx, "manage_tenant")
    try:
        await set_principal_disabled(ctx.tenant_id, principal_id, False)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await AuditService().append(
        tenant_id=ctx.tenant_id,
        actor_principal_id=ctx.principal_id,
        action="user:reactivate",
        resource_type="principal",
        resource_id=principal_id,
    )
    return {"disabled": False}

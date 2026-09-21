"""The first real protected route (T0.6) — demonstrates full RequestContext resolution
and doubles as the route-inventory test's proof that the mechanism actually blocks
unauthenticated access, not just that it compiles.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.platform_admin import is_platform_admin
from api.dependencies import get_db_session
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from core.tenancy.admin import ADMIN_TENANT_ID
from core.tenancy.context import RequestContext
from core.tenancy.models import Identity, Principal, Tenant

router = APIRouter(
    tags=["me"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


class MeResponse(BaseModel):
    principal_id: uuid.UUID
    tenant_id: uuid.UUID
    display_name: str
    email: str | None = None
    tenant_slug: str
    tenant_name: str = ""
    # True only for owner/admin members of the reserved admin tenant -- the signal the
    # web shell keys its admin navigation on (the server still re-checks every call).
    platform_admin: bool
    # Whether this session is in the reserved admin organization, as opposed to merely
    # being allowed to administer. A single-tenant owner is both an ordinary user and a
    # platform admin, and their home is their workspace -- only a dedicated admin-tenant
    # account has nothing else to land on.
    admin_tenant: bool


@router.get("/me")
async def get_me(
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    principal = await session.get(Principal, ctx.principal_id)
    assert principal is not None  # get_request_context already verified this
    tenant = await session.get(Tenant, ctx.tenant_id)
    platform_admin = await is_platform_admin(ctx.tenant_id, ctx.principal_id)
    email = await session.scalar(
        select(Identity.external_id).where(
            Identity.provider == "local", Identity.principal_id == ctx.principal_id
        )
    )
    return MeResponse(
        principal_id=ctx.principal_id,
        tenant_id=ctx.tenant_id,
        display_name=principal.display_name,
        email=email,
        tenant_slug=tenant.slug if tenant is not None else "",
        tenant_name=tenant.name if tenant is not None else "",
        platform_admin=platform_admin,
        admin_tenant=ctx.tenant_id == ADMIN_TENANT_ID,
    )


class UpdateMeRequest(BaseModel):
    display_name: str


@router.patch("/me")
async def update_me(
    body: UpdateMeRequest,
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    principal = await session.get(Principal, ctx.principal_id)
    assert principal is not None
    name = body.display_name.strip()
    if not name:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="display name cannot be empty")
    principal.display_name = name
    await session.flush()
    return await get_me(ctx, session)

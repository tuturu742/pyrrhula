"""Tenant resolution for the *pre-authentication* surface (register/login) — before a
JWT exists, the client has to say which tenant it means to reach. ``X-Pyrrhula-Tenant``
(a tenant slug) does that; when absent and ``PYRRHULA_SINGLE_TENANT_UI=true``, the
configured default tenant is used instead (plan §13.8: single-tenant mode is a feature
flag over the same multi-tenant core, not a different build).

Authenticated routes do **not** use this — see ``middleware/auth.py``: once a JWT exists,
the tenant comes from the token's signed claims, not a client-supplied header, precisely
so a token issued for tenant A can't be pointed at tenant B by changing a header.
"""

from __future__ import annotations

from fastapi import Header, HTTPException
from sqlalchemy import select

from core.config import get_settings
from core.tenancy.models import Tenant
from core.tenancy.scope import unscoped_session


async def resolve_tenant_for_auth(
    x_pyrrhula_tenant: str | None = Header(default=None),
) -> Tenant:
    settings = get_settings()
    default_slug = settings.default_tenant_slug if settings.single_tenant_ui else None
    slug = x_pyrrhula_tenant or default_slug
    if not slug:
        raise HTTPException(
            status_code=400,
            detail="X-Pyrrhula-Tenant header required (or enable PYRRHULA_SINGLE_TENANT_UI)",
        )

    async with unscoped_session() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"unknown tenant: {slug!r}")
    if tenant.is_library:
        # A1.10 (D13): the library tenant has no memberships and must never gain one --
        # this is the explicit guard, not just an absence this could otherwise slip past.
        raise HTTPException(status_code=404, detail=f"unknown tenant: {slug!r}")
    if tenant.deactivated_at is not None:
        # A deactivated tenant (set from the admin console) accepts no logins.
        raise HTTPException(status_code=403, detail="tenant is deactivated")
    return tenant

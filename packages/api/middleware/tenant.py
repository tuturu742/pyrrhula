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
from core.tenancy.admin import ADMIN_TENANT_SLUG
from core.tenancy.models import Tenant
from core.tenancy.scope import unscoped_session


async def _sole_tenant() -> Tenant | None:
    """The one tenant a single-tenant deployment means, when it has exactly one.

    Single-tenant mode used to resolve to a *configured slug* that defaulted to ``dev``
    -- a tenant no installer has ever created. Compose and Kubernetes both turn the mode
    on by default, so out of the box it promised "no tenant header required" and answered
    every header-less login with ``404 unknown tenant: 'dev'``. Inferring it is what the
    operator meant by switching the mode on: they are declaring there is one.

    Never guesses between several. Two tenants means the deployment is not, in fact,
    single-tenant, and picking one would sign somebody into the wrong organization.

    The reserved admin tenant does not count: it exists on every install, so counting it
    would mean a solo deployment always looked like two.
    """
    async with unscoped_session() as session:
        tenants = (
            (
                await session.execute(
                    select(Tenant).where(
                        Tenant.is_library.is_(False),
                        Tenant.slug != ADMIN_TENANT_SLUG,
                        Tenant.deactivated_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
    return tenants[0] if len(tenants) == 1 else None


async def resolve_tenant_for_auth(
    x_pyrrhula_tenant: str | None = Header(default=None),
) -> Tenant:
    settings = get_settings()
    slug = x_pyrrhula_tenant or (
        settings.default_tenant_slug if settings.single_tenant_ui else None
    )
    if not slug:
        if settings.single_tenant_ui:
            # No header, no configured slug: infer the sole tenant.
            sole = await _sole_tenant()
            if sole is not None:
                return sole
            raise HTTPException(
                status_code=400,
                detail=(
                    "single-tenant mode cannot tell which tenant this is: the deployment "
                    "has no organization yet, or has more than one. Sign up first, or set "
                    "PYRRHULA_DEFAULT_TENANT_SLUG, or send X-Pyrrhula-Tenant."
                ),
            )
        raise HTTPException(
            status_code=400,
            detail="X-Pyrrhula-Tenant header required (or enable PYRRHULA_SINGLE_TENANT_UI)",
        )

    async with unscoped_session() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
    if tenant is None:
        # A configured slug that does not exist is an operator mistake, not a dead end:
        # if the deployment has exactly one tenant, that is unambiguously the one meant.
        if settings.single_tenant_ui and not x_pyrrhula_tenant:
            sole = await _sole_tenant()
            if sole is not None:
                return sole
        raise HTTPException(status_code=404, detail=f"unknown tenant: {slug!r}")
    if tenant.is_library:
        # A1.10 (D13): the library tenant has no memberships and must never gain one --
        # this is the explicit guard, not just an absence this could otherwise slip past.
        raise HTTPException(status_code=404, detail=f"unknown tenant: {slug!r}")
    if tenant.deactivated_at is not None:
        # A deactivated tenant (set from the admin console) accepts no logins.
        raise HTTPException(status_code=403, detail="tenant is deactivated")
    return tenant

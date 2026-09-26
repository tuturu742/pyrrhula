"""Principal resolution for authenticated routes: token -> principal -> tenant
membership check -> ``RequestContext``.

The tenant comes from the JWT's signed claims, never from a client-supplied header — a
token issued for tenant A must not be usable against tenant B just by changing
``X-Pyrrhula-Tenant``. If that header *is* present anyway, it must agree with the token's
tenant or the request is rejected; this is what makes "a principal from tenant A
presenting a valid token cannot address tenant B's context" a 403, not a mix-up.

Membership is re-checked on every request (not just at token-issue time), so a
membership revoked mid-session, or a principal disabled mid-session, takes effect
immediately rather than only after the token naturally expires.
"""

from __future__ import annotations

from fastapi import Cookie, Header, HTTPException
from sqlalchemy import select

from api.auth.tokens import InvalidTokenError, is_revoked, verify_token
from core.tenancy.context import RequestContext
from core.tenancy.models import Membership, Principal, Tenant
from core.tenancy.scope import tenant_scope


def _extract_token(authorization: str | None, session_cookie: str | None) -> str:
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value
    if session_cookie:
        return session_cookie
    raise HTTPException(status_code=401, detail="not authenticated")


async def get_request_context(
    authorization: str | None = Header(default=None),
    pyrrhula_session: str | None = Cookie(default=None),
    x_pyrrhula_tenant: str | None = Header(default=None),
) -> RequestContext:
    token = _extract_token(authorization, pyrrhula_session)

    try:
        claims = verify_token(token)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="invalid or expired token") from exc

    # Revoked at logout: a stateless JWT is otherwise valid until its own expiry, so a
    # leaked cookie would outlive the user's "log out" by up to a day.
    if await is_revoked(claims):
        raise HTTPException(status_code=401, detail="token has been revoked")

    async with tenant_scope(claims.tenant_id) as session:
        if x_pyrrhula_tenant is not None:
            tenant = await session.scalar(select(Tenant).where(Tenant.slug == x_pyrrhula_tenant))
            if tenant is None or tenant.id != claims.tenant_id:
                raise HTTPException(
                    status_code=403,
                    detail="token tenant does not match X-Pyrrhula-Tenant",
                )

        # A tenant deactivated mid-session (from the admin console) is rejected immediately,
        # not only when its principals' tokens expire -- same rationale as the disabled-
        # principal check below.
        this_tenant = await session.get(Tenant, claims.tenant_id)
        if this_tenant is not None and this_tenant.deactivated_at is not None:
            raise HTTPException(status_code=403, detail="tenant is deactivated")

        principal = await session.get(Principal, claims.principal_id)
        if principal is None or principal.disabled_at is not None:
            raise HTTPException(status_code=403, detail="principal disabled or not found")

        membership = await session.scalar(
            select(Membership).where(
                Membership.tenant_id == claims.tenant_id,
                Membership.principal_id == claims.principal_id,
            )
        )
        if membership is None:
            raise HTTPException(status_code=403, detail="no membership in this tenant")

    return RequestContext(principal_id=claims.principal_id, tenant_id=claims.tenant_id)

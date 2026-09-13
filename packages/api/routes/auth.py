"""Register/login/logout (T0.6). Local password auth via IdentityProvider (T0.3).

Self-registration grants ``role='viewer'`` — the least-privilege default. Granting
'owner' happens via the tenant-provisioning flow (``core.tenancy.seed`` today; a real
provisioning API is out of T0.6's scope), not self-service registration.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import select

from adapters.identity.local.argon2_provider import LocalArgon2IdentityProvider
from api.auth.tokens import issue_token
from api.middleware.rate_limit import rate_limit_by_ip
from api.middleware.tenant import resolve_tenant_for_auth
from core.config import get_settings
from core.tenancy.models import Identity, Membership, Principal, Tenant
from core.tenancy.scope import tenant_scope

router = APIRouter(prefix="/auth", tags=["auth"])
_identity_provider = LocalArgon2IdentityProvider()

_SESSION_COOKIE = "pyrrhula_session"


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        _SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        # PYRRHULA_COOKIE_SECURE=true behind TLS: the browser then refuses to send this
        # cookie over plain http, which is the whole point of terminating TLS.
        secure=get_settings().cookie_secure,
        max_age=60 * 60 * 24,
    )


@router.post("/register", dependencies=[Depends(rate_limit_by_ip)])
async def register(
    body: RegisterRequest,
    response: Response,
    tenant: Tenant = Depends(resolve_tenant_for_auth),
) -> TokenResponse:
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="password must be at least 8 characters")

    # Best-effort pre-check for the common case (a clean 409 without creating anything).
    # register_local()'s unique constraint is still the source of truth for the race —
    # see the cleanup below if two concurrent registrations for the same email both pass
    # this check.
    async with tenant_scope(tenant.id) as session:
        existing = await session.scalar(
            select(Identity.id).where(
                Identity.provider == "local", Identity.external_id == body.email
            )
        )
    if existing is not None:
        raise HTTPException(status_code=409, detail="email already registered")

    async with tenant_scope(tenant.id) as session:
        principal = Principal(tenant_id=tenant.id, kind="human", display_name=body.display_name)
        session.add(principal)
        await session.flush()
        session.add(Membership(tenant_id=tenant.id, principal_id=principal.id, role="viewer"))
        principal_id = principal.id

    try:
        await _identity_provider.register_local(tenant.id, principal_id, body.email, body.password)
    except Exception as exc:
        # Identity creation lost the race (duplicate email) -- the principal/membership
        # created above are now orphaned; remove them rather than leaving a dangling
        # tenant member with no way to authenticate.
        async with tenant_scope(tenant.id) as session:
            orphaned_principal = await session.get(Principal, principal_id)
            if orphaned_principal is not None:
                await session.delete(orphaned_principal)
        raise HTTPException(status_code=409, detail="email already registered") from exc

    token = issue_token(principal_id=principal_id, tenant_id=tenant.id)
    _set_session_cookie(response, token)
    return TokenResponse(access_token=token)


class SignupRequest(BaseModel):
    organization: str
    email: EmailStr
    display_name: str
    password: str


class SignupResponse(BaseModel):
    access_token: str
    tenant_slug: str


def _slugify(name: str) -> str:
    import re as _re

    slug = _re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:40]
    return slug or "org"


@router.post("/signup", dependencies=[Depends(rate_limit_by_ip)])
async def signup(body: SignupRequest, response: Response) -> SignupResponse:
    """Self-serve organization signup: a new tenant + its owner in one step -- tenant,
    default workspace + scopes, owner principal/membership, login identity, and the
    owner's overseer workspace membership (session conducting/inspection is gated on a
    workspace role, which tenant ownership alone does not grant)."""
    from core.config import get_settings
    from core.tenancy.models import WorkspaceMembership
    from core.tenancy.provisioning import (
        TenantExistsError,
        create_tenant,
        create_tenant_user,
        delete_principal,
    )

    if not get_settings().allow_tenant_signup:
        raise HTTPException(status_code=403, detail="signup is disabled on this deployment")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="password must be at least 8 characters")
    if not body.organization.strip():
        raise HTTPException(status_code=400, detail="organization name is required")

    base_slug = _slugify(body.organization)
    tenant_id = workspace_id = None
    slug = base_slug
    for _attempt in range(6):
        try:
            tenant_id, workspace_id = await create_tenant(body.organization.strip(), slug)
            break
        except TenantExistsError:
            import secrets as _secrets

            slug = f"{base_slug}-{_secrets.token_hex(2)}"
    if tenant_id is None:
        raise HTTPException(status_code=409, detail="could not allocate an organization slug")

    principal_id = await create_tenant_user(tenant_id, body.display_name, "owner")
    try:
        await _identity_provider.register_local(tenant_id, principal_id, body.email, body.password)
    except Exception as exc:
        await delete_principal(tenant_id, principal_id)
        raise HTTPException(status_code=409, detail="email already registered") from exc

    async with tenant_scope(tenant_id) as session:
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal_id,
                role="steward",
            )
        )

    # A launchable flow out of the box: the domain-neutral round table (tenant-level,
    # visible in every workspace's picker). Best-effort -- signup never fails on it.
    try:
        import copy as _copy

        from core.process.authoring import create_definition
        from core.process.dsl.fixtures import AGENT_ROUND_TABLE_FLOW

        definition = _copy.deepcopy(AGENT_ROUND_TABLE_FLOW)
        definition["name"] = "Round Table"
        await create_definition(
            tenant_id, "round_table", "Round Table", definition, created_by=principal_id
        )
    except Exception:  # noqa: BLE001 -- a flow can be authored later; the account matters
        pass

    token = issue_token(principal_id=principal_id, tenant_id=tenant_id)
    _set_session_cookie(response, token)
    return SignupResponse(access_token=token, tenant_slug=slug)


async def _audit_auth(tenant_id: uuid.UUID, principal_id: uuid.UUID, action: str) -> None:
    """Best-effort auth audit: never block a login on the audit path being slow, but
    never silently skip it either (the failure is logged)."""
    try:
        from core.audit.service import AuditService

        await AuditService().append(
            tenant_id=tenant_id,
            actor_principal_id=principal_id,
            action=action,
            resource_type="principal",
            resource_id=principal_id,
        )
    except Exception as exc:  # noqa: BLE001
        import structlog

        structlog.get_logger().warning("audit.auth_failed", action=action, error=str(exc)[:200])


@router.post("/login", dependencies=[Depends(rate_limit_by_ip)])
async def login(
    body: LoginRequest,
    response: Response,
    tenant: Tenant = Depends(resolve_tenant_for_auth),
) -> TokenResponse:
    identity = await _identity_provider.verify_local(tenant.id, body.email, body.password)
    if identity is None:
        raise HTTPException(status_code=401, detail="invalid email or password")

    token = issue_token(principal_id=identity.principal_id, tenant_id=tenant.id)
    _set_session_cookie(response, token)
    # Trust-ops: authentication is an auditable event -- the hash-chained log is the
    # record of who touched this tenant and when, and a login is the first link.
    await _audit_auth(tenant.id, identity.principal_id, "auth:login")
    return TokenResponse(access_token=token)


@router.post("/logout")
async def logout(
    response: Response,
    authorization: str | None = Header(default=None),
    pyrrhula_session: str | None = Cookie(default=None),
) -> dict[str, bool]:
    """Clears the cookie AND revokes the presented token, so logging out actually ends
    the session rather than merely forgetting it client-side."""
    from api.auth.tokens import InvalidTokenError, revoke_token, verify_token

    raw = ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            raw = value
    raw = raw or (pyrrhula_session or "")
    if raw:
        try:
            claims = verify_token(raw)
            await revoke_token(claims)
            await _audit_auth(claims.tenant_id, claims.principal_id, "auth:logout")
        except InvalidTokenError:
            pass  # already invalid: nothing to revoke
    response.delete_cookie(_SESSION_COOKIE)
    return {"ok": True}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password", dependencies=[Depends(rate_limit_by_ip)])
async def change_password(
    body: ChangePasswordRequest,
    authorization: str | None = Header(default=None),
    pyrrhula_session: str | None = Cookie(default=None),
) -> dict[str, bool]:
    """Verify the current password, store a fresh argon2 hash, revoke the presented
    token (a password change should end the session that made it -- the client signs
    back in), and audit. The audit found NO password-change path existed anywhere,
    backend included: a user needing a new password had to be deleted and recreated."""
    from argon2 import PasswordHasher
    from sqlalchemy import select as sa_select

    from api.auth.tokens import InvalidTokenError, revoke_token, verify_token
    from core.tenancy.models import Identity
    from core.tenancy.scope import tenant_scope

    raw = ""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            raw = value
    raw = raw or (pyrrhula_session or "")
    try:
        claims = verify_token(raw)
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="not signed in") from exc

    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="password must be at least 8 characters")

    hasher = PasswordHasher()
    async with tenant_scope(claims.tenant_id) as session:
        identity = (
            (
                await session.execute(
                    sa_select(Identity).where(
                        Identity.provider == "local",
                        Identity.principal_id == claims.principal_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if identity is None or not identity.password_hash:
            raise HTTPException(status_code=404, detail="no local login on this account")
        try:
            hasher.verify(identity.password_hash, body.current_password)
        except Exception as exc:  # noqa: BLE001 -- argon2's mismatch exception taxonomy
            raise HTTPException(status_code=403, detail="current password is wrong") from exc
        identity.password_hash = hasher.hash(body.new_password)

    await revoke_token(claims)
    await _audit_auth(claims.tenant_id, claims.principal_id, "auth:password_change")
    return {"ok": True}

"""JWT issuance/verification for local sessions. Stateless: there is no
server-side revocation list in v1 — logout clears the client's cookie, but a token
already issued remains valid until it expires. Acceptable for a dev-stage MVP; a
revocation/deny-list is a Phase-5-adjacent hardening item, not a Phase-0 requirement.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import jwt

from core.config import get_settings


class InvalidTokenError(Exception):
    pass


@dataclass(frozen=True)
class TokenClaims:
    principal_id: uuid.UUID
    tenant_id: uuid.UUID
    jti: str = ""
    expires_at: int = 0


def issue_token(*, principal_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    settings = get_settings()
    now = int(time.time())
    payload = {
        "sub": str(principal_id),
        "tenant_id": str(tenant_id),
        "iat": now,
        "exp": now + settings.jwt_expiry_seconds,
        # A per-token id so logout can actually revoke THIS token: without it a stolen
        # cookie stays valid until natural expiry and "log out" is a client-side lie.
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_token(token: str) -> TokenClaims:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return TokenClaims(
            principal_id=uuid.UUID(payload["sub"]),
            tenant_id=uuid.UUID(payload["tenant_id"]),
            jti=str(payload.get("jti", "")),
            expires_at=int(payload.get("exp", 0)),
        )
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise InvalidTokenError(str(exc)) from exc


# ── revocation (jti denylist in Redis, TTL'd to the token's own expiry) ──────────────
_REVOKED_PREFIX = "revoked_jti:"


async def revoke_token(claims: TokenClaims) -> None:
    """Deny this token for whatever remains of its lifetime. Redis-backed: the denylist
    is small (only tokens revoked before expiry) and self-cleaning (TTL = the token's
    own exp), so a stateless-JWT deployment gains real logout without gaining a session
    table."""
    if not claims.jti:
        return  # pre-jti token: nothing to revoke individually
    from api.redis_client import get_redis

    ttl = max(1, claims.expires_at - int(time.time()))
    await get_redis().setex(f"{_REVOKED_PREFIX}{claims.jti}", ttl, "1")


async def is_revoked(claims: TokenClaims) -> bool:
    if not claims.jti:
        return False
    from api.redis_client import get_redis

    try:
        return bool(await get_redis().exists(f"{_REVOKED_PREFIX}{claims.jti}"))
    except Exception:  # noqa: BLE001 -- Redis down must not lock everyone out
        return False

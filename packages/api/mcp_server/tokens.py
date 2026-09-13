"""MCP tool-call tokens (E2.12, §9.1, §13.7): `(tenant, workspace, principal)` claims --
one more than the HTTP session token (`api.auth.tokens.TokenClaims`), since a human's HTTP
request carries its own workspace in the URL/body while an MCP tool call has no such
per-call channel, so the token itself must carry it.

Reuses the HTTP session token's JWT secret/settings (`core.config.get_settings()`) rather
than a second signing secret to rotate for no isolation benefit, but tags every MCP token
with `"aud": "mcp"` -- PyJWT then rejects an MCP token handed to `api.auth.tokens
.verify_token` (unexpected audience claim) and rejects an HTTP session token handed to
`verify_mcp_token` here (missing audience claim) in both directions, so the two token
kinds can't be silently cross-used even though they share a secret.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import jwt

from core.config import get_settings

_AUDIENCE = "mcp"


class InvalidMcpTokenError(Exception):
    pass


@dataclass(frozen=True)
class McpTokenClaims:
    tenant_id: uuid.UUID
    workspace_id: uuid.UUID
    principal_id: uuid.UUID


def issue_mcp_token(
    *, tenant_id: uuid.UUID, workspace_id: uuid.UUID, principal_id: uuid.UUID
) -> str:
    settings = get_settings()
    now = int(time.time())
    payload = {
        "sub": str(principal_id),
        "tenant_id": str(tenant_id),
        "workspace_id": str(workspace_id),
        "aud": _AUDIENCE,
        "iat": now,
        "exp": now + settings.jwt_expiry_seconds,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_mcp_token(token: str) -> McpTokenClaims:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            audience=_AUDIENCE,
        )
        return McpTokenClaims(
            tenant_id=uuid.UUID(payload["tenant_id"]),
            workspace_id=uuid.UUID(payload["workspace_id"]),
            principal_id=uuid.UUID(payload["sub"]),
        )
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise InvalidMcpTokenError(str(exc)) from exc

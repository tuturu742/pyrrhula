"""Share tokens for public preview links.

A preview link is meant to be *sent to someone* -- a tester with no Pyrrhula account --
so the route that serves it cannot require a session. That creates a problem the rest of
the codebase never has: the request arrives with no tenant context, and every
tenant-scoped table is behind RLS with FORCE. ``unscoped_session()`` is not an escape
hatch here; it sets no ``app.tenant_id``, so an RLS-covered table returns zero rows, and
adding ``preview_environment`` to the isolation suite's exception list would weaken the
one guarantee this system is built on.

So the token carries its own tenancy: a JWT signed with the deployment secret, holding
the tenant id and preview id. The route decodes it, recovers the tenant, and then opens
an ordinary ``tenant_scope(tenant_id)`` like every other read in the codebase. Nothing
about the isolation model changes -- the token is just an authenticated way to learn
which tenant to scope to.

Same construction as ``core.repos.service.mint_git_job_token``, and deliberately a
distinct ``use`` claim so a preview token can never be replayed as a git credential.
"""

from __future__ import annotations

import uuid

_USE = "preview"


def mint_preview_token(tenant_id: uuid.UUID, preview_id: uuid.UUID, *, ttl_seconds: int) -> str:
    """A share link's credential. Scope: exactly one preview, in one tenant."""
    import time as _time

    import jwt as _jwt

    from core.config import get_settings

    settings = get_settings()
    return _jwt.encode(
        {
            "use": _USE,
            "tid": str(tenant_id),
            "pid": str(preview_id),
            "exp": int(_time.time()) + ttl_seconds,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def read_preview_token(token: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    """(tenant_id, preview_id) for a valid, unexpired preview token; None otherwise.

    Returning None rather than raising keeps the public route's failure path a plain
    404 -- an invalid token and a missing preview should be indistinguishable to someone
    guessing links."""
    import jwt as _jwt

    from core.config import get_settings

    settings = get_settings()
    try:
        claims = _jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except _jwt.PyJWTError:
        return None
    if claims.get("use") != _USE:
        return None
    try:
        return uuid.UUID(str(claims["tid"])), uuid.UUID(str(claims["pid"]))
    except (KeyError, ValueError):
        return None

"""Inference job tokens: what a coding harness authenticates to Pyrrhula with.

A harness running in an exec environment has to call a model. The alternative to this is
handing the container a decrypted provider key, which ``agent.credential_ref`` exists
precisely to prevent: a key in a container that runs agent-chosen commands is a key that
has left the building, cannot be scoped, cannot be revoked, and spends without being
metered.

So the container gets a token instead, and the model call comes back to us. The shape is
deliberately the one ``core.repos.service.mint_git_job_token`` already established for git
smart-HTTP -- a JWT signed with the deployment secret, carrying a ``use`` claim so no
verifier accepts another's token, short-lived, and never persisted anywhere. The claims
here name *whose* spend this is, because the proxy meters and rate-limits against them
rather than trusting anything the harness sends.

The model is deliberately NOT a claim. A harness asks for a model by name in its request
body; the proxy ignores that and uses the connection this token names. One place decides
what a persona's harness is allowed to spend on.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import jwt

from core.config import get_settings

USE = "inference"


@dataclass(frozen=True)
class InferenceGrant:
    """Who a verified inference token says is spending, and on whose behalf."""

    tenant_id: uuid.UUID
    session_id: uuid.UUID
    persona_id: uuid.UUID
    agent_id: uuid.UUID


def mint_inference_job_token(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    persona_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    ttl_seconds: int,
) -> str:
    """A credential for one persona's harness, for the length of one delegation.

    ``ttl_seconds`` is the caller's to choose and should be the engine's run timeout, not
    a comfortable round number: the token is only useful while the container is running,
    and a token that outlives its container is a spending credential nobody is watching.
    """
    settings = get_settings()
    return jwt.encode(
        {
            "use": USE,
            "t": str(tenant_id),
            "s": str(session_id),
            "p": str(persona_id),
            "a": str(agent_id),
            "exp": int(time.time()) + ttl_seconds,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def verify_inference_job_token(token: str) -> InferenceGrant | None:
    """The grant this token carries, or ``None`` if it is not a valid inference token.

    Returns the grant rather than a boolean because every claim is load-bearing
    downstream -- the tenant scopes the database session, the connection decides the
    model and the key, and the persona and session are what the usage row is attributed
    to. A caller that only learned "valid" would have to take the rest from the request
    body, which is exactly what a token is for avoiding.
    """
    settings = get_settings()
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    if claims.get("use") != USE:
        return None
    try:
        return InferenceGrant(
            tenant_id=uuid.UUID(str(claims["t"])),
            session_id=uuid.UUID(str(claims["s"])),
            persona_id=uuid.UUID(str(claims["p"])),
            agent_id=uuid.UUID(str(claims["a"])),
        )
    except (KeyError, ValueError):
        return None

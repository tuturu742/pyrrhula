"""`overseer.query` MCP tool: the MCP surface's only secret-plaintext
read path -- a thin wrapper over `OverseerService.inspect()`, the same permission check and
the same audit-in-transaction path as the HTTP `/overseer/secrets/{id}` route
(`packages/api/overseer/routes.py`). This file must never import `core.secrets.repo`
directly -- INV-1 reserves that import for `core.assembler`/`core.overseer`, and
`OverseerService` is the only boundary this tool is allowed to cross to reach it
(`tests/architecture/test_mcp_overseer_query_boundary.py` guards this).

Full MCP transport/protocol wiring (tool registration, JSON-RPC framing) lives in
``api.mcp_server.build`` -- this is the tool's own logic, callable with a
resolved `(tenant, workspace, principal)` token, ahead of the server that will eventually
dispatch to it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from api.mcp_server.tokens import InvalidMcpTokenError, verify_mcp_token
from core.overseer.service import (
    OverseerPermissionDeniedError,
    OverseerService,
    SecretNotFoundError,
)
from core.ports.encryptor import Encryptor
from core.ports.permission import PermissionService

__all__ = [
    "McpToolAuthError",
    "OverseerQueryResult",
    "SecretNotFoundError",
    "overseer_query",
]

_RESULT_LABEL_KEY = "entity.secret"


class McpToolAuthError(Exception):
    """Raised for both an invalid/expired token and a token whose principal lacks
    `secret:inspect` -- the MCP surface has no HTTP-status-code channel to distinguish
    401 from 403 over, and from the caller's perspective both mean "you may not do
    this"."""


@dataclass(frozen=True)
class OverseerQueryResult:
    id: uuid.UUID
    label_key: str
    subject_kind: str
    subject_kind_label_key: str
    subject_id: uuid.UUID
    content: str
    gist: str
    hint_text: str | None
    behavioral_directive: str | None
    disclosure_state: str
    disclosure_state_label_key: str


async def overseer_query(
    token: str,
    secret_id: uuid.UUID,
    *,
    encryptor: Encryptor,
    permission_service: PermissionService,
) -> OverseerQueryResult:
    """One tool call = one `OverseerService.inspect()` call. The tenant/workspace/
    principal come only from the token's own signed claims, never from a caller-supplied
    argument -- a compromised MCP client can't widen its own scope by passing a
    different id alongside its token."""
    try:
        claims = verify_mcp_token(token)
    except InvalidMcpTokenError as exc:
        raise McpToolAuthError(str(exc)) from exc

    service = OverseerService(encryptor=encryptor, permission_service=permission_service)
    try:
        view = await service.inspect(
            claims.tenant_id,
            claims.principal_id,
            claims.workspace_id,
            secret_id,
            query={"surface": "mcp", "tool": "overseer.query"},
        )
    except OverseerPermissionDeniedError as exc:
        raise McpToolAuthError(str(exc)) from exc

    return OverseerQueryResult(
        id=view.id,
        label_key=_RESULT_LABEL_KEY,
        subject_kind=view.subject_kind,
        subject_kind_label_key=f"subject_kind.{view.subject_kind}",
        subject_id=view.subject_id,
        content=view.content,
        gist=view.gist,
        hint_text=view.hint_text,
        behavioral_directive=view.behavioral_directive,
        disclosure_state=view.disclosure_state,
        disclosure_state_label_key=f"disclosure_state.{view.disclosure_state}",
    )

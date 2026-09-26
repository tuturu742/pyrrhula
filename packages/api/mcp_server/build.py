"""Composition of the MCP surface.

Split from `server.py` so the dispatch machinery has no opinion about which tools exist,
and from the tool modules so none of them knows about the others. The result is that
"what is on the MCP surface" is answered in exactly one readable place.

`overseer.query` is registered here rather than living apart, even though E2.12 built it
first: one surface, one list. Its handler still crosses only `OverseerService`, which
`tests/architecture/test_mcp_overseer_query_boundary.py` guards independently.
"""

from __future__ import annotations

import uuid
from typing import Any

from api.encryptor_factory import get_encryptor
from api.mcp_server.server import McpServer, McpTool
from api.mcp_server.tokens import McpTokenClaims, issue_mcp_token
from api.mcp_server.tools.deterministic import deterministic_tools
from api.mcp_server.tools.entity_tools import ENTITY_MUTATE, ENTITY_READ
from api.mcp_server.tools.knowledge_query import KNOWLEDGE_QUERY
from api.mcp_server.tools.overseer_query import overseer_query
from api.permission_service_factory import get_permission_service


async def _overseer_query(claims: McpTokenClaims, arguments: dict[str, Any]) -> dict[str, Any]:
    """Adapts the handler, which takes a raw token, to the dispatcher's
    already-verified claims. Re-issuing rather than changing that function's signature
    keeps its own tests meaningful and its boundary lint pointed at the same file."""
    token = issue_mcp_token(
        tenant_id=claims.tenant_id,
        workspace_id=claims.workspace_id,
        principal_id=claims.principal_id,
    )
    result = await overseer_query(
        token,
        uuid.UUID(str(arguments["secret_id"])),
        encryptor=get_encryptor(),
        permission_service=get_permission_service(),
    )
    return {
        "id": str(result.id),
        "label_key": result.label_key,
        "subject_kind": result.subject_kind,
        "content": result.content,
    }


OVERSEER_QUERY = McpTool(
    name="overseer.query",
    description="Read a secret's plaintext. Requires secret:inspect; writes an audit row.",
    parameters={
        "type": "object",
        "properties": {"secret_id": {"type": "string", "format": "uuid"}},
        "required": ["secret_id"],
    },
    handler=_overseer_query,
)


async def build_server(tenant_id: uuid.UUID) -> McpServer:
    """The tool list is per-tenant because the deterministic half of it is registry-driven:
    a tenant that registered a new deterministic tool has it, and one that didn't doesn't.
    Building per call is cheap and removes a cache that could serve a stale list."""
    server = McpServer()
    for tool in (ENTITY_READ, ENTITY_MUTATE, KNOWLEDGE_QUERY, OVERSEER_QUERY):
        server.register(tool)
    for tool in await deterministic_tools(tenant_id):
        server.register(tool)
    return server

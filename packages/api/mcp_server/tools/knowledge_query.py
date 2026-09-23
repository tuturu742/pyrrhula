"""`knowledge.query` over MCP (G4.13, plan §9.1, §13.7, INV-4).

**INV-4 applies to MCP callers identically.** The scope set is resolved from the *token's*
principal and pushed down as a SQL predicate by the same retrieval path a session turn
uses. There is no parameter by which a caller supplies scopes, because a caller-supplied
scope is a caller-chosen answer to the one question that must never be caller-chosen.

This deliberately does not import `core.knowledge.repo` -- INV-1 reserves that for the
assembler and the overseer, and the retrieval modules (`core.knowledge.retrieval.*`) are
the sanctioned path the plan names for exactly this tool.
"""

from __future__ import annotations

from typing import Any

from api.mcp_server.server import McpTool, require_str
from api.mcp_server.tokens import McpTokenClaims
from core.assembler.visibility import EXPORT, scopes_for
from core.knowledge.retrieval.rerank import fetch_chunk_texts
from core.knowledge.retrieval.sparse import search_sparse
from core.knowledge.retrieval.versions import effective_version_ids

_DEFAULT_LIMIT = 10
_MAX_LIMIT = 50


async def _knowledge_query(claims: McpTokenClaims, arguments: dict[str, Any]) -> dict[str, Any]:
    query_text = require_str(arguments, "query")
    class_ = str(arguments.get("class") or "lore")
    limit = min(int(arguments.get("limit") or _DEFAULT_LIMIT), _MAX_LIMIT)

    scope_set = await scopes_for(
        claims.tenant_id, claims.principal_id, claims.workspace_id, EXPORT, None
    )
    if not scope_set:
        # A principal with no relationship to the workspace resolves to no scopes at all,
        # and an empty scope set means an empty result -- never "unfiltered".
        return {"query": query_text, "class": class_, "hits": []}

    # Resolved from the workspace's attachments for the same reason the scope set is
    # resolved from the token: a caller who could name a version could name a superseded
    # one, and read text this workspace has already replaced.
    version_ids = await effective_version_ids(claims.tenant_id, claims.workspace_id)
    if not version_ids:
        return {"query": query_text, "class": class_, "hits": []}

    hits = await search_sparse(
        tenant_id=claims.tenant_id,
        scope_keys=scope_set,
        class_=class_,
        version_ids=version_ids,
        query_text=query_text,
        k=limit,
    )
    texts = await fetch_chunk_texts(claims.tenant_id, [h.chunk_id for h in hits])
    return {
        "query": query_text,
        "class": class_,
        "hits": [
            {
                "chunk_id": str(h.chunk_id),
                "entry_key": h.entry_key,
                "source_id": str(h.source_id),
                "version_id": str(h.version_id) if h.version_id else None,
                "score": h.score,
                "text": texts.get(h.chunk_id, ""),
            }
            for h in hits
        ],
    }


KNOWLEDGE_QUERY = McpTool(
    name="knowledge.query",
    description=(
        "Search knowledge visible to this token's principal. Scope filtering is applied in "
        "SQL and cannot be overridden by the caller."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "class": {"type": "string", "default": "lore"},
            "limit": {"type": "integer", "default": _DEFAULT_LIMIT, "maximum": _MAX_LIMIT},
        },
        "required": ["query"],
    },
    handler=_knowledge_query,
)

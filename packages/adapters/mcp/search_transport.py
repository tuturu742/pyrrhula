"""``McpTransport`` for the ``web_search`` server: a SearXNG metasearch backend.

Fills in the preset ``core.mcp.registry.WEB_SEARCH_PRESET`` reserved ("web search is just
an MCP server behind a workspace policy flag" -- no special code path): the workspace's
registered ``web_search`` row carries the SearXNG base URL, this transport offers the one
``search`` tool, and the workspace allowlist remains the egress control.

Results are DATA the model reads, never instructions -- the caller (core.mcp.client /
the in-session tool handler) wraps them in the injection envelope like every other tool
output. Only title/URL/snippet are returned, trimmed hard.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from core.ports.mcp import McpServerRef, McpToolResult, McpToolSpec, McpTransportError

SEARCH_TOOL = McpToolSpec(
    name="search",
    description=(
        "Search the internet. Returns the top results as title, URL and snippet. "
        "Use for current facts, docs, or anything outside your training data."
    ),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "what to search for"}},
        "required": ["query"],
    },
)

# The default engine set favours reliability over breadth: big engines CAPTCHA-block fresh
# self-hosted SearXNG IPs quickly; bing tolerates them. Overridable per deployment.
_DEFAULT_ENGINES = os.environ.get("PYRRHULA_WEB_SEARCH_ENGINES", "bing")
_MAX_RESULTS = 5


class SearxngSearchTransport:
    async def list_tools(self, server: McpServerRef) -> list[McpToolSpec]:  # noqa: ARG002
        return [SEARCH_TOOL]

    async def call_tool(
        self, server: McpServerRef, name: str, arguments: dict[str, Any]
    ) -> McpToolResult:
        if name != "search":
            raise McpTransportError(f"unknown tool {name!r} on {server.key!r}")
        query = str(arguments.get("query") or "").strip()
        if not query:
            return McpToolResult(content="empty query", is_error=True)
        base = (server.url or "").rstrip("/")
        if not base.startswith("http"):
            raise McpTransportError(
                f"web_search server {server.key!r} has no usable url ({server.url!r})"
            )
        params = {"q": query, "format": "json"}
        if _DEFAULT_ENGINES:
            params["engines"] = _DEFAULT_ENGINES
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(f"{base}/search", params=params)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise McpTransportError(f"search backend unreachable: {exc}") from exc

        results = []
        for item in (data.get("results") or [])[:_MAX_RESULTS]:
            results.append(
                {
                    "title": str(item.get("title") or "")[:200],
                    "url": str(item.get("url") or "")[:300],
                    "snippet": str(item.get("content") or "")[:400],
                }
            )
        if not results:
            return McpToolResult(content=f'no results for "{query}"', structured={"results": []})
        lines = [
            f"{i + 1}. {r['title']}\n   {r['url']}\n   {r['snippet']}"
            for i, r in enumerate(results)
        ]
        return McpToolResult(
            content="\n".join(lines), structured={"results": results, "query": query}
        )

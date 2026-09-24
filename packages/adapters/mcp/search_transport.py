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
        "properties": {
            "query": {"type": "string", "description": "what to search for"},
            "recency": {
                "type": "string",
                "enum": ["day", "week", "month", "year", "any"],
                "description": (
                    "only results published within this window. Use 'week' or 'day' for "
                    "news -- without it the top results are whatever ranks best, which "
                    "for a well-covered subject is usually years old."
                ),
            },
        },
        "required": ["query"],
    },
)

# Which engines an instance can actually use is a fact about that instance, so a
# registration overrides this with `options: {"engines": "..."}` and two tenants running
# two instances share no such fact.
#
# The default was `bing`, on the reasoning that the big engines CAPTCHA-block a fresh
# self-hosted address and bing tolerates it. Measured here, bing returns nothing at all,
# and a search that returns nothing is indistinguishable from a model that did not
# search. Empty is the honest default: it means "whatever this instance has enabled",
# which is the instance's own decision and is at least visible in its settings.
_DEFAULT_ENGINES = ""
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
        # SearXNG's own filter, so the engine does the narrowing rather than the caller
        # reading dates off a page of stale hits. Observed: an unfiltered query for a
        # well-covered subject returned results spanning ten years, and the model picked
        # from the top; the same query with a week's window returned that day's.
        recency = str(arguments.get("recency") or "").strip().lower()
        if recency and recency != "any":
            params["time_range"] = recency
        engines = str(server.options.get("engines") or _DEFAULT_ENGINES)
        if engines:
            params["engines"] = engines
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

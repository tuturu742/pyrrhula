"""One ``McpTransport`` that routes by server key.

``core.mcp.client`` iterates a workspace's registered servers calling one transport; with
more than one server *kind* (git delegation, web search) the composition root hands it this
router instead of a single-purpose transport. Routing is by the registered key: the
``web_search`` preset key goes to the search backend, ``resolution`` in-process,
``git``/``git-*`` to the git store. Any OTHER server whose url is plain http(s) is a
real external MCP endpoint and goes to the generic remote client (M-C) -- the admin
attaches these per tenant, packs may declare them. Everything else falls to the
unreachable stub (contributes no tools, fails loudly on call -- the semantics
``core.mcp.client`` already handles).
"""

from __future__ import annotations

from typing import Any

from adapters.mcp.http_transport import UnreachableMcpTransport
from core.ports.mcp import McpServerRef, McpToolResult, McpToolSpec, McpTransport

_PRESET_KEYS = ("web_search", "resolution", "git")


class KeyRoutingMcpTransport:
    def __init__(
        self,
        *,
        git: McpTransport | None = None,
        web_search: McpTransport | None = None,
        resolution: McpTransport | None = None,
        remote: McpTransport | None = None,
    ) -> None:
        self._git = git
        self._web_search = web_search
        self._resolution = resolution
        self._remote = remote
        self._fallback = UnreachableMcpTransport()

    def _route(self, server: McpServerRef) -> McpTransport:
        if server.key == "web_search" and self._web_search is not None:
            return self._web_search
        if server.key == "resolution" and self._resolution is not None:
            return self._resolution
        if (server.key == "git" or server.key.startswith("git-")) and self._git is not None:
            return self._git
        if (
            self._remote is not None
            and server.key not in _PRESET_KEYS
            and server.url.startswith(("http://", "https://"))
        ):
            return self._remote
        return self._fallback

    async def list_tools(self, server: McpServerRef) -> list[McpToolSpec]:
        return await self._route(server).list_tools(server)

    async def call_tool(
        self, server: McpServerRef, name: str, arguments: dict[str, Any]
    ) -> McpToolResult:
        return await self._route(server).call_tool(server, name, arguments)

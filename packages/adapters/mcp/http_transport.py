"""v1 `McpTransport`: a deliberately unreachable stub.

There is no MCP server in this environment to talk to, and a transport that pretended
otherwise would be untested code in the one place where "it looked like it worked" is most
expensive. So this raises `McpTransportError` on every call, which is exactly what
`core.mcp.client` already handles: an unreachable server contributes no tools, and a call
to one fails loudly rather than silently succeeding.

Swapping in a real streamable-HTTP/stdio MCP client is a change to
`worker.mcp_transport_factory` and nothing else -- the allowlist, the phase policy, the
idempotency, and the injection envelope all live in core and are already tested against a
scripted double.
"""

from __future__ import annotations

from typing import Any

from core.ports.mcp import McpServerRef, McpToolResult, McpToolSpec, McpTransportError


class UnreachableMcpTransport:
    async def list_tools(self, server: McpServerRef) -> list[McpToolSpec]:
        raise McpTransportError(
            f"no MCP transport is configured in this deployment; cannot reach {server.key!r} "
            f"at {server.url}"
        )

    async def call_tool(
        self, server: McpServerRef, name: str, arguments: dict[str, Any]
    ) -> McpToolResult:
        del arguments
        raise McpTransportError(
            f"no MCP transport is configured in this deployment; cannot call {name!r} on "
            f"{server.key!r}"
        )

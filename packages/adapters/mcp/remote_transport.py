"""Generic remote MCP client over streamable HTTP (M-C): the transport that makes a
registered external server actually reachable.

Speaks JSON-RPC 2.0 per the MCP streamable-HTTP transport: ``initialize`` (capturing
the ``mcp-session-id`` header when the server issues one), ``notifications/initialized``,
then ``tools/list`` / ``tools/call``. Responses may arrive as plain JSON or as an SSE
stream (FastMCP's default); both are parsed. Every safety property lives OUTSIDE this
adapter -- allowlist, phase policy, idempotency, effectful confirmation, and the
injection envelope are ``core.mcp``'s job -- so this stays a dumb, bounded pipe:

- connect/read timeouts (read from the server's own ``timeout_seconds``, default 120 --
  engine tools legitimately run tests/builds, a lookup tool should fail fast);
- result size cap (the server's ``max_result_chars``, default 100k) so a
  misbehaving server cannot flood a context window;
- ``credential_ref`` names an ENVIRONMENT VARIABLE holding the bearer token (e.g.
  ``credential_ref: "MY_MCP_TOKEN"`` sends ``Authorization: Bearer $MY_MCP_TOKEN``).
  A pointer, never a key, same rule as everywhere else; per-tenant sealed secrets can
  replace this resolver later without touching callers.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx

from core.ports.mcp import McpServerRef, McpToolResult, McpToolSpec, McpTransportError

_PROTOCOL_VERSION = "2025-03-26"


# Defaults for a registration that sets no limit of its own. Generous on time because
# engine tools legitimately run builds and tests; the per-server field is how a fast
# server says it should fail fast.
_DEFAULT_READ_TIMEOUT_S = 120.0
_DEFAULT_MAX_RESULT_CHARS = 100_000


def _parse_body(response: httpx.Response) -> dict[str, Any] | None:
    """A JSON-RPC response object from either a JSON body or an SSE stream. For SSE,
    the LAST ``data:`` payload that parses as a JSON-RPC response wins (progress
    notifications may precede it)."""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        result: dict[str, Any] | None = None
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload:
                continue
            try:
                parsed = json.loads(payload)
            except ValueError:
                continue
            if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
                result = parsed
        return result
    try:
        parsed = response.json()
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


class RemoteMcpTransport:
    """One short-lived MCP session per operation: initialize -> operate -> drop. The
    handshake costs one round-trip and buys statelessness -- no session cache to go
    stale when a sidecar restarts between turns.

    ``httpx_transport`` exists for tests (an ``httpx.MockTransport`` standing in for
    the network); production callers pass nothing."""

    def __init__(self, *, httpx_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._httpx_transport = httpx_transport

    async def list_tools(self, server: McpServerRef) -> list[McpToolSpec]:
        result = await self._request(server, "tools/list", {})
        specs: list[McpToolSpec] = []
        for tool in result.get("tools", []):
            if not isinstance(tool, dict) or not tool.get("name"):
                continue
            specs.append(
                McpToolSpec(
                    name=str(tool["name"]),
                    description=str(tool.get("description") or ""),
                    parameters=dict(tool.get("inputSchema") or {}),
                )
            )
        return specs

    async def call_tool(
        self, server: McpServerRef, name: str, arguments: dict[str, Any]
    ) -> McpToolResult:
        result = await self._request(server, "tools/call", {"name": name, "arguments": arguments})
        parts: list[str] = []
        for item in result.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        content = "\n".join(parts)
        cap = server.max_result_chars or _DEFAULT_MAX_RESULT_CHARS
        if len(content) > cap:
            content = content[:cap] + f"\n[truncated at {cap} characters]"
        structured = result.get("structuredContent")
        return McpToolResult(
            content=content,
            is_error=bool(result.get("isError", False)),
            structured=dict(structured) if isinstance(structured, dict) else {},
        )

    # ── wire ─────────────────────────────────────────────────────────────────────────
    def _headers(self, server: McpServerRef) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if server.credential_ref:
            token = os.environ.get(server.credential_ref, "")
            if token:
                headers["authorization"] = f"Bearer {token}"
        return headers

    async def _request(
        self, server: McpServerRef, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        read = float(server.timeout_seconds or _DEFAULT_READ_TIMEOUT_S)
        timeout = httpx.Timeout(connect=5.0, read=read, write=10.0, pool=5.0)
        headers = self._headers(server)
        try:
            async with httpx.AsyncClient(
                timeout=timeout, transport=self._httpx_transport
            ) as client:
                init = await client.post(
                    server.url,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": str(uuid.uuid4()),
                        "method": "initialize",
                        "params": {
                            "protocolVersion": _PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": {"name": "pyrrhula", "version": "1.0"},
                        },
                    },
                )
                init.raise_for_status()
                init_body = _parse_body(init)
                if init_body is None or "error" in init_body:
                    raise McpTransportError(
                        f"mcp server {server.key!r}: initialize failed "
                        f"({(init_body or {}).get('error') or init.status_code})"
                    )
                session_id = init.headers.get("mcp-session-id")
                if session_id:
                    headers = {**headers, "mcp-session-id": session_id}
                await client.post(
                    server.url,
                    headers=headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
                response = await client.post(
                    server.url,
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": str(uuid.uuid4()),
                        "method": method,
                        "params": params,
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise McpTransportError(f"mcp server {server.key!r}: {exc}") from exc

        body = _parse_body(response)
        if body is None:
            raise McpTransportError(f"mcp server {server.key!r}: unintelligible {method} response")
        if "error" in body:
            error = body["error"]
            message = error.get("message", error) if isinstance(error, dict) else error
            raise McpTransportError(f"mcp server {server.key!r}: {method} error: {message}")
        result = body.get("result")
        if not isinstance(result, dict):
            raise McpTransportError(
                f"mcp server {server.key!r}: {method} returned no result object"
            )
        return result

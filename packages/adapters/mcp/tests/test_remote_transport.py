"""RemoteMcpTransport against a fake MCP server (httpx.MockTransport): handshake,
tools/list and tools/call mapping, SSE bodies, auth header from the env-named ref,
error surfaces, and the result size cap."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from adapters.mcp.remote_transport import RemoteMcpTransport
from core.ports.mcp import McpServerRef, McpTransportError

_SERVER = McpServerRef(key="engine", url="http://fake-mcp:8090/mcp")


class FakeMcpServer:
    """Speaks just enough streamable-HTTP MCP for the client under test."""

    def __init__(self, *, sse: bool = False, expect_token: str | None = None) -> None:
        self.sse = sse
        self.expect_token = expect_token
        self.calls: list[dict[str, Any]] = []

    def _respond(self, body: dict[str, Any]) -> httpx.Response:
        if self.sse:
            text = f"data: {json.dumps(body)}\n\n"
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if (
            self.expect_token is not None
            and request.headers.get("authorization") != f"Bearer {self.expect_token}"
        ):
            return httpx.Response(401, json={"detail": "no token"})
        payload = json.loads(request.content)
        method = payload.get("method")
        self.calls.append(payload)
        if method == "initialize":
            return self._respond(
                {"jsonrpc": "2.0", "id": payload["id"], "result": {"capabilities": {}}}
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return self._respond(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "run_tests",
                                "description": "run the test suite",
                                "inputSchema": {"type": "object", "properties": {}},
                            },
                            {"name": "export_build", "description": "export"},
                        ]
                    },
                }
            )
        if method == "tools/call":
            name = payload["params"]["name"]
            if name == "explode":
                return self._respond(
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "error": {"code": -32000, "message": "kaboom"},
                    }
                )
            if name == "huge":
                return self._respond(
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {"content": [{"type": "text", "text": "x" * 200_000}]},
                    }
                )
            return self._respond(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "content": [{"type": "text", "text": f"{name} ok"}],
                        "isError": False,
                        "structuredContent": {"exit_code": 0},
                    },
                }
            )
        return httpx.Response(400, json={"detail": f"unexpected method {method}"})


def _transport(server: FakeMcpServer) -> RemoteMcpTransport:
    return RemoteMcpTransport(httpx_transport=httpx.MockTransport(server.handler))


async def test_list_tools_maps_specs() -> None:
    fake = FakeMcpServer()
    specs = await _transport(fake).list_tools(_SERVER)
    assert [s.name for s in specs] == ["run_tests", "export_build"]
    assert specs[0].parameters == {"type": "object", "properties": {}}
    # handshake happened before the listing
    assert [c.get("method") for c in fake.calls][:2] == [
        "initialize",
        "notifications/initialized",
    ]


async def test_call_tool_maps_result_and_structured() -> None:
    result = await _transport(FakeMcpServer()).call_tool(_SERVER, "run_tests", {})
    assert result.content == "run_tests ok"
    assert result.is_error is False
    assert result.structured == {"exit_code": 0}


async def test_sse_bodies_are_parsed() -> None:
    result = await _transport(FakeMcpServer(sse=True)).call_tool(_SERVER, "run_tests", {})
    assert result.content == "run_tests ok"


async def test_jsonrpc_error_raises_transport_error() -> None:
    with pytest.raises(McpTransportError, match="kaboom"):
        await _transport(FakeMcpServer()).call_tool(_SERVER, "explode", {})


async def test_result_size_cap() -> None:
    result = await _transport(FakeMcpServer()).call_tool(_SERVER, "huge", {})
    assert len(result.content) < 200_000
    assert "truncated" in result.content


async def test_bearer_token_from_env_named_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGINE_MCP_TOKEN", "s3cret")
    server = McpServerRef(
        key="engine", url="http://fake-mcp:8090/mcp", credential_ref="ENGINE_MCP_TOKEN"
    )
    fake = FakeMcpServer(expect_token="s3cret")
    result = await _transport(fake).call_tool(server, "run_tests", {})
    assert result.content == "run_tests ok"


async def test_unreachable_server_raises_transport_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = RemoteMcpTransport(httpx_transport=httpx.MockTransport(refuse))
    with pytest.raises(McpTransportError):
        await transport.list_tools(_SERVER)

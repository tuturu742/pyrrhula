"""``commit_asset``: a generated image reaches the repo as real bytes.

A model cannot emit a PNG through a text channel, so it names a URL and the *server*
fetches it. That makes the origin check the whole security story, and it fails closed:
with no server-injected allowlist, every URL is refused.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import httpx
import pytest

from adapters.mcp.git_store import GitStore
from adapters.mcp.git_transport import GitMcpTransport
from core.ports.mcp import McpServerRef, McpTransportError

# GitMcpTransport._repo() takes the store repo key straight off the ref's url.
_SERVER = McpServerRef(key="git", url="proj")
_ORIGIN = "http://comfy-mcp:8091"


def _png(width: int = 2, height: int = 2) -> bytes:
    """A real, minimal PNG -- so the test proves bytes survive git, not just that some
    blob round-trips."""
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _transport(tmp_path: Path, response: httpx.Response | None = None) -> GitMcpTransport:
    transport = GitMcpTransport(GitStore(str(tmp_path)))
    if response is not None:
        original = httpx.AsyncClient.get

        async def fake_get(self, url, *a, **kw):  # noqa: ANN001, ARG001
            return response

        httpx.AsyncClient.get = fake_get  # type: ignore[method-assign]
        transport._restore_get = lambda: setattr(httpx.AsyncClient, "get", original)  # type: ignore[attr-defined]
    return transport


@pytest.fixture
def restore_httpx():
    original = httpx.AsyncClient.get
    yield
    httpx.AsyncClient.get = original  # type: ignore[method-assign]


async def test_commits_a_real_png_as_bytes(tmp_path: Path, restore_httpx) -> None:
    payload = _png()
    transport = _transport(
        tmp_path, httpx.Response(200, content=payload, headers={"content-type": "image/png"})
    )
    result = await transport.call_tool(
        _SERVER,
        "commit_asset",
        {
            "asset_url": f"{_ORIGIN}/assets/cat.png",
            "repo_path": "assets/cat.png",
            "allowed_origins": [_ORIGIN],
        },
    )

    assert result.structured["bytes"] == len(payload)
    on_disk = tmp_path / "proj" / "repo" / "assets" / "cat.png"
    assert on_disk.read_bytes() == payload, "the committed file is not byte-identical"
    assert on_disk.read_bytes().startswith(b"\x89PNG"), "not a valid PNG after commit"


async def test_unregistered_origin_is_refused(tmp_path: Path, restore_httpx) -> None:
    transport = _transport(
        tmp_path, httpx.Response(200, content=_png(), headers={"content-type": "image/png"})
    )
    with pytest.raises(McpTransportError, match="not a registered MCP server"):
        await transport.call_tool(
            _SERVER,
            "commit_asset",
            {
                "asset_url": "http://evil.example/x.png",
                "repo_path": "assets/cat.png",
                "allowed_origins": [_ORIGIN],
            },
        )


async def test_fails_closed_with_no_allowlist(tmp_path: Path, restore_httpx) -> None:
    """The allowlist is injected server-side. If a model reaches this tool without one,
    it must not become a general-purpose fetcher."""
    transport = _transport(
        tmp_path, httpx.Response(200, content=_png(), headers={"content-type": "image/png"})
    )
    with pytest.raises(McpTransportError, match="not a registered MCP server"):
        await transport.call_tool(
            _SERVER,
            "commit_asset",
            {
                "asset_url": f"{_ORIGIN}/assets/cat.png",
                "repo_path": "assets/cat.png",
            },
        )


@pytest.mark.parametrize(
    "bad_path",
    ["../escape.png", "/etc/passwd", "assets/../../x.png", ".hidden/x.png", "a b/c.png"],
)
async def test_unsafe_paths_are_refused(tmp_path: Path, bad_path: str, restore_httpx) -> None:
    transport = _transport(
        tmp_path, httpx.Response(200, content=_png(), headers={"content-type": "image/png"})
    )
    with pytest.raises(McpTransportError, match="unsafe asset path"):
        await transport.call_tool(
            _SERVER,
            "commit_asset",
            {
                "asset_url": f"{_ORIGIN}/x.png",
                "repo_path": bad_path,
                "allowed_origins": [_ORIGIN],
            },
        )


async def test_non_image_content_type_is_refused(tmp_path: Path, restore_httpx) -> None:
    """The generator returns images. An HTML error page rendered as a sprite would be a
    confusing, silent failure at build time."""
    transport = _transport(
        tmp_path,
        httpx.Response(200, content=b"<html>nope</html>", headers={"content-type": "text/html"}),
    )
    with pytest.raises(McpTransportError, match="unsupported asset content-type"):
        await transport.call_tool(
            _SERVER,
            "commit_asset",
            {
                "asset_url": f"{_ORIGIN}/x.png",
                "repo_path": "assets/cat.png",
                "allowed_origins": [_ORIGIN],
            },
        )


async def test_upstream_error_surfaces(tmp_path: Path, restore_httpx) -> None:
    transport = _transport(tmp_path, httpx.Response(404, content=b""))
    with pytest.raises(McpTransportError, match="asset fetch returned 404"):
        await transport.call_tool(
            _SERVER,
            "commit_asset",
            {
                "asset_url": f"{_ORIGIN}/missing.png",
                "repo_path": "assets/cat.png",
                "allowed_origins": [_ORIGIN],
            },
        )


async def test_commit_on_a_branch_keeps_main_clean(tmp_path: Path, restore_httpx) -> None:
    payload = _png()
    transport = _transport(
        tmp_path, httpx.Response(200, content=payload, headers={"content-type": "image/png"})
    )
    result = await transport.call_tool(
        _SERVER,
        "commit_asset",
        {
            "asset_url": f"{_ORIGIN}/cat.png",
            "repo_path": "assets/cat.png",
            "branch": "pyr/art-1",
            "allowed_origins": [_ORIGIN],
        },
    )
    assert result.structured["branch"] == "pyr/art-1"
    assert result.structured["commit"]
    # commit_on_branch returns to main, which must not carry the asset.
    assert not (tmp_path / "proj" / "repo" / "assets" / "cat.png").exists()

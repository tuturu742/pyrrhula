"""Socket preview adapter: container creation shape, without an engine socket.

``_request`` is the single seam to the engine's REST API, so the tests drive it directly
rather than standing up a fake unix socket.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from adapters.preview.docker_socket import DockerSocketPreviewProvider
from core.ports.preview import PreviewUnavailableError


class _Recorder:
    """Replaces ``_request``; records calls and replays scripted responses."""

    def __init__(self, responses: dict[tuple[str, str], httpx.Response]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, method: str, path: str, *, json_body=None, params=None, headers=None, timeout=120.0
    ) -> httpx.Response:
        self.calls.append({"method": method, "path": path, "json": json_body, "params": params})
        for (m, prefix), response in self.responses.items():
            if method == m and path.startswith(prefix):
                return response
        return httpx.Response(200, json={})

    def created_body(self) -> dict[str, Any]:
        for call in self.calls:
            if call["path"] == "/containers/create":
                return call["json"]
        raise AssertionError("no container was created")


def _provider(recorder: _Recorder) -> DockerSocketPreviewProvider:
    provider = DockerSocketPreviewProvider("/nonexistent.sock", network="pyrrhula-envs")
    provider._request = recorder  # type: ignore[method-assign]
    return provider


async def test_start_publishes_no_host_port() -> None:
    """The proxy is the only ingress. A PortBindings entry here would expose an
    unauthenticated preview directly on the host, bypassing the share token entirely."""
    recorder = _Recorder(
        {
            ("GET", "/containers/"): httpx.Response(404, json={}),  # nothing to replace
            ("POST", "/images/create"): httpx.Response(200, text=""),
            ("POST", "/containers/create"): httpx.Response(201, json={"Id": "abc"}),
        }
    )
    handle = await _provider(recorder).start(
        "pyr-prev-abcd1234",
        "python:3.12-slim",
        "serve",
        env={"PYR_ARTIFACT_TOKEN": "tok"},
        port=8080,
    )

    body = recorder.created_body()
    assert "PortBindings" not in body["HostConfig"]
    assert body["ExposedPorts"] == {"8080/tcp": {}}
    # On the engine's shared network, so the api container can reach it by name.
    assert body["HostConfig"]["NetworkMode"] == "pyrrhula-envs"
    assert handle.internal_url == "http://pyr-prev-abcd1234:8080"


async def test_start_passes_the_token_as_an_env_var_not_in_the_command() -> None:
    recorder = _Recorder(
        {
            ("GET", "/containers/"): httpx.Response(404, json={}),
            ("POST", "/images/create"): httpx.Response(200, text=""),
            ("POST", "/containers/create"): httpx.Response(201, json={"Id": "abc"}),
        }
    )
    await _provider(recorder).start(
        "pyr-prev-x", "img", "serve-command", env={"PYR_ARTIFACT_TOKEN": "sekrit"}, port=8080
    )
    body = recorder.created_body()
    assert "PYR_ARTIFACT_TOKEN=sekrit" in body["Env"]
    assert "sekrit" not in " ".join(body["Cmd"])


async def test_existing_container_is_replaced_not_reused() -> None:
    """A reused container would keep serving the previous build's files."""
    recorder = _Recorder(
        {
            ("GET", "/containers/"): httpx.Response(200, json={"State": {"Running": True}}),
            ("POST", "/images/create"): httpx.Response(200, text=""),
            ("POST", "/containers/create"): httpx.Response(201, json={"Id": "abc"}),
        }
    )
    await _provider(recorder).start("pyr-prev-x", "img", "serve", env={}, port=8080)
    assert any(c["method"] == "DELETE" for c in recorder.calls), "old container not removed"
    assert any(c["path"] == "/containers/create" for c in recorder.calls)


async def test_pull_error_is_reported_as_unavailable() -> None:
    recorder = _Recorder(
        {
            ("GET", "/containers/"): httpx.Response(404, json={}),
            ("POST", "/images/create"): httpx.Response(500, text="no such image"),
        }
    )
    with pytest.raises(PreviewUnavailableError, match="could not pull"):
        await _provider(recorder).start("pyr-prev-x", "nope", "serve", env={}, port=8080)


async def test_status_maps_engine_state() -> None:
    running = _Recorder(
        {("GET", "/containers/"): httpx.Response(200, json={"State": {"Running": True}})}
    )
    missing = _Recorder({("GET", "/containers/"): httpx.Response(404, json={})})
    crashed = _Recorder(
        {
            ("GET", "/containers/"): httpx.Response(
                200, json={"State": {"Running": False, "ExitCode": 1}}
            )
        }
    )

    assert await _provider(running).status("pyr-prev-x") == "running"
    assert await _provider(missing).status("pyr-prev-x") == "missing"
    assert await _provider(crashed).status("pyr-prev-x") == "failed"

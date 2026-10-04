"""A script on a socket engine may run as long as the engine's run timeout.

The exec stream is one HTTP call that lasts as long as the command. It used to share the
600-second timeout of every other engine call, so a harness working for eleven minutes was
cut off -- and reported as "engine socket unreachable: " with nothing after the colon.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from adapters.exec_env.docker_socket import DockerSocketExecEnvProvider
from core.ports.exec_env import ExecEnvUnavailableError


class _Recorder(DockerSocketExecEnvProvider):
    def __init__(self, **kw: Any) -> None:
        super().__init__("/nonexistent.sock", **kw)
        self.timeouts: dict[str, float] = {}

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        self.timeouts[path.split("/")[1] + ":" + path.rsplit("/", 1)[-1]] = kw.get("timeout", 600.0)
        request = httpx.Request(method, f"http://d{path}")
        if path.endswith("/exec") and method == "POST":
            return httpx.Response(201, json={"Id": "e1"}, request=request)
        if path.endswith("/json"):
            return httpx.Response(200, json={"ExitCode": 0}, request=request)
        return httpx.Response(200, content=b"", request=request)


async def test_the_exec_stream_waits_for_the_run_timeout() -> None:
    provider = _Recorder(run_timeout_seconds=3600)
    await provider.exec("env", "true")
    assert provider.timeouts["exec:start"] > 3600


async def test_the_default_outlasts_the_old_ten_minute_cap() -> None:
    provider = _Recorder()
    await provider.exec("env", "true")
    assert provider.timeouts["exec:start"] > 1800


async def test_a_timeout_says_so() -> None:
    provider = DockerSocketExecEnvProvider("/nonexistent.sock")

    class _Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("", request=request)

    provider._client = lambda timeout=600.0: httpx.AsyncClient(transport=_Slow())  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(ExecEnvUnavailableError) as raised:
        await provider._request("POST", "/exec/e1/start", timeout=1920.0)  # noqa: SLF001
    message = str(raised.value)
    assert "unreachable" not in message
    assert "1920 seconds" in message and "ReadTimeout" in message

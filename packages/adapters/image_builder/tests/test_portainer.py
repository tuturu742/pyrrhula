"""The Portainer builder against a mock Docker-through-Portainer API."""

from __future__ import annotations

import base64
import io
import json
import tarfile

import httpx
import pytest

from adapters.image_builder.portainer import PortainerConfig, PortainerImageBuilder, push_ref
from core.ports.image_builder import BuildSpec

KEY = "ptr_SENTINEL_" + "k" * 30
PUSH_PW = "push-SENTINEL-pw"
DIGEST = "sha256:" + "a" * 64
_SPEC = BuildSpec(
    build_id="b1",
    dockerfile="FROM debian:bookworm\nRUN apt-get install -y git\n",
    target_ref="192.168.8.50:5002/pyr/tabc/godot:hash-12345678",
    labels={"pyrrhula.build": "b1"},
)


class Engine:
    def __init__(self, *, build_error: str = "", build_lines: int = 3) -> None:
        self.build_error = build_error
        self.build_lines = build_lines
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.headers["x-api-key"] == KEY
        path = request.url.path
        if path.endswith("/docker/build"):
            lines = [{"stream": f"Step {i}/3 : RUN something\n"} for i in range(self.build_lines)]
            lines.append({"stream": f"leaky line with {KEY}\n"})
            if self.build_error:
                lines.append(
                    {"errorDetail": {"message": self.build_error}, "error": self.build_error}
                )
            body = "\n".join(json.dumps(x) for x in lines)
            return httpx.Response(200, text=body)
        if path.endswith("/push"):
            body = "\n".join(
                json.dumps(x)
                for x in (
                    {"status": "Pushing"},
                    {"progressDetail": {}, "aux": {"Tag": "x", "Digest": DIGEST, "Size": 1}},
                )
            )
            return httpx.Response(200, text=body)
        if request.method == "DELETE":
            return httpx.Response(200, json=[])
        if path.endswith("/docker/info"):
            return httpx.Response(200, json={"ServerVersion": "27.5.0", "NCPU": 16})
        return httpx.Response(404)


def _builder(engine: Engine, **config: object) -> PortainerImageBuilder:
    return PortainerImageBuilder(
        PortainerConfig(base_url="https://portainer.lan:9443", endpoint_id=3, **config),  # type: ignore[arg-type]
        api_key=KEY,
        push_username="pyrbuild",
        push_password=PUSH_PW,
        transport=httpx.MockTransport(engine),
        guard=False,
    )


async def _run(builder: PortainerImageBuilder, cancel: bool = False):  # type: ignore[no-untyped-def]
    logs: list[str] = []

    async def on_log(text: str) -> None:
        logs.append(text)

    async def should_cancel() -> bool:
        return cancel

    progress = await builder.run(
        _SPEC, on_log=on_log, should_cancel=should_cancel, timeout_seconds=600
    )
    return progress, logs


async def test_a_build_and_push_report_the_pushed_digest() -> None:
    engine = Engine()
    progress, logs = await _run(
        _builder(engine, push_host="127.0.0.1:5002", network_mode="pyr-builds")
    )
    assert progress.state == "succeeded" and progress.digest == DIGEST

    build, push, delete = engine.calls
    assert build.url.params["t"] == "127.0.0.1:5002/pyr/tabc/godot:hash-12345678"
    assert build.url.params["networkmode"] == "pyr-builds"
    assert int(build.url.params["memory"]) == 4096 * 2**20
    with tarfile.open(fileobj=io.BytesIO(build.content)) as tar:
        assert tar.getnames() == ["Dockerfile"], "the build context is the Dockerfile alone"
    assert "x-registry-auth" not in build.headers, "the push credential is for the push only"

    assert push.url.path.endswith("/images/127.0.0.1:5002/pyr/tabc/godot/push")
    assert push.url.params["tag"] == "hash-12345678"
    auth = json.loads(base64.b64decode(push.headers["x-registry-auth"]))
    assert auth == {"username": "pyrbuild", "password": PUSH_PW, "serveraddress": "127.0.0.1:5002"}
    assert delete.method == "DELETE"
    assert not any(KEY in line for line in logs) and KEY not in progress.log_tail


async def test_a_failing_step_fails_the_build_without_pushing() -> None:
    engine = Engine(build_error="The command '/bin/sh -c apt-get install' returned 100")
    progress, _ = await _run(_builder(engine))
    assert progress.state == "failed" and "returned 100" in progress.error
    assert not any(c.url.path.endswith("/push") for c in engine.calls)


async def test_cancel_closes_the_stream() -> None:
    engine = Engine(build_lines=50)
    progress, _ = await _run(_builder(engine), cancel=True)
    assert progress.state == "cancelled"
    assert not any(c.url.path.endswith("/push") for c in engine.calls)


async def test_probe_reads_the_engine() -> None:
    probe = await _builder(Engine()).probe()
    assert probe.ok and "27.5.0" in probe.detail


def test_push_ref_swaps_only_the_host() -> None:
    assert push_ref("192.168.8.50:5002/a/b:t", "127.0.0.1:5002") == "127.0.0.1:5002/a/b:t"
    assert push_ref("ghcr.io/a/b:t", "") == "ghcr.io/a/b:t"


async def test_the_portainer_url_must_be_https() -> None:
    from core.ports.image_builder import BuilderError

    builder = PortainerImageBuilder(
        PortainerConfig(base_url="http://portainer.lan:9000", endpoint_id=3),
        api_key=KEY,
        transport=httpx.MockTransport(Engine()),
        guard=False,
    )
    with pytest.raises(BuilderError, match="https"):
        await _run(builder)

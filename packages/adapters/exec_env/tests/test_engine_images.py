"""Pulls, warm-container reuse and pod hardening -- the Phase 0 image fixes, no engine."""

from __future__ import annotations

import json
from typing import Any

import httpx

from adapters.exec_env.docker_socket import DockerSocketExecEnvProvider
from adapters.exec_env.engine_images import pull_params
from adapters.exec_env.kubernetes import KubernetesExecEnvProvider

_DIGEST = "sha256:" + "b" * 64


def test_an_untagged_legacy_row_pulls_latest_not_every_tag() -> None:
    """Untagged references are refused where they are typed now, but rows stored before
    that still exist. Without an explicit tag the engine pulls every tag."""
    assert pull_params("barichello/godot-ci") == {
        "fromImage": "barichello/godot-ci",
        "tag": "latest",
    }
    assert pull_params("python:3.12") == {"fromImage": "python:3.12"}
    assert pull_params(f"ghcr.io/o/i@{_DIGEST}") == {"fromImage": f"ghcr.io/o/i@{_DIGEST}"}


class _FakeEngine:
    """Just enough of the Docker API for provision(): inspect, delete, pull, create, start."""

    def __init__(self, existing_labels: dict[str, str] | None) -> None:
        self.existing_labels = existing_labels
        self.calls: list[tuple[str, str]] = []
        self.created: dict[str, Any] | None = None

    async def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        self.calls.append((method, path))
        if method == "GET" and path.endswith("/json"):
            if self.existing_labels is None:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={"Config": {"Labels": self.existing_labels}, "State": {"Running": True}},
            )
        if method == "POST" and path == "/containers/create":
            self.created = kw.get("json_body")
            return httpx.Response(201, json={})
        return httpx.Response(204 if method == "DELETE" else 200, text="")


def _provider(engine: _FakeEngine) -> DockerSocketExecEnvProvider:
    provider = DockerSocketExecEnvProvider("/nonexistent.sock")
    provider._request = engine.request  # type: ignore[method-assign]  # noqa: SLF001
    return provider


async def test_a_warm_container_for_the_same_image_is_reused() -> None:
    probe = _provider(_FakeEngine(None))
    labels = probe._identity_labels("img:1")  # noqa: SLF001
    engine = _FakeEngine(labels)
    assert await _provider(engine).provision("pyr-env-x", "img:1", binds=[], setup_cmds=[]) == (
        "pyr-env-x"
    )
    assert ("POST", "/containers/create") not in engine.calls


async def test_a_rebuilt_image_replaces_the_warm_container() -> None:
    """The name says which session and repo; it never said which image. A rebuilt image
    used to be ignored for the rest of the session."""
    probe = _provider(_FakeEngine(None))
    stale = probe._identity_labels("img:1")  # noqa: SLF001
    engine = _FakeEngine(stale)
    await _provider(engine).provision("pyr-env-x", "img:2", binds=[], setup_cmds=[])
    assert ("DELETE", "/containers/pyr-env-x") in engine.calls
    assert engine.created is not None
    assert engine.created["Labels"]["pyrrhula.image"] == "img:2"


async def test_a_container_from_before_the_labels_is_replaced_too() -> None:
    engine = _FakeEngine({"pyrrhula.exec_env": "1"})
    await _provider(engine).provision("pyr-env-x", "img:1", binds=[], setup_cmds=[])
    assert ("DELETE", "/containers/pyr-env-x") in engine.calls


def _k8s_job(image: str) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            captured.update(json.loads(request.content))
            return httpx.Response(201, json={})
        if request.url.path.endswith("/pods"):
            name = captured["metadata"]["name"]
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"name": f"{name}-p"},
                            "status": {
                                "phase": "Succeeded",
                                "containerStatuses": [{"state": {"terminated": {"exitCode": 0}}}],
                            },
                        }
                    ]
                },
            )
        return httpx.Response(200, text="ok")

    engine = {
        "key": "k",
        "kind": "kubernetes",
        "namespace": "envs",
        "api_base": "https://k8s.test",
        "token": "t",
        "run_timeout_seconds": 600,
    }
    provider = KubernetesExecEnvProvider(engine, transport=httpx.MockTransport(handler))
    return provider, captured  # type: ignore[return-value]


async def test_an_agent_pod_gets_no_api_credential_and_a_deadline() -> None:
    provider, captured = _k8s_job("img:1")  # type: ignore[misc]
    await provider.run_script("pyr-env-x", "img:1", "echo hi")
    spec = captured["spec"]
    pod = spec["template"]["spec"]
    assert spec["activeDeadlineSeconds"] == 660, "the cluster stops a run nobody is watching"
    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is False
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    container = pod["containers"][0]
    assert container["securityContext"]["capabilities"]["drop"] == ["NET_RAW"]
    assert "imagePullPolicy" not in container, "a tag keeps Kubernetes' own default"


async def test_a_digest_pinned_image_may_use_the_node_cache() -> None:
    provider, captured = _k8s_job(f"ghcr.io/o/i@{_DIGEST}")  # type: ignore[misc]
    await provider.run_script("pyr-env-x", f"ghcr.io/o/i@{_DIGEST}", "echo hi")
    container = captured["spec"]["template"]["spec"]["containers"][0]
    assert container["imagePullPolicy"] == "IfNotPresent"

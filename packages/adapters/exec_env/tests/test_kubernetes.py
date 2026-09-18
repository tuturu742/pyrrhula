"""Kubernetes adapter against ``httpx.MockTransport``: Job create → pod poll → logs →
exit code; teardown by label. No cluster."""

from __future__ import annotations

import json

import httpx
import pytest

from adapters.exec_env.kubernetes import KubernetesExecEnvProvider
from core.ports.exec_env import ExecEnvUnavailableError

_ENGINE = {
    "key": "k8s",
    "kind": "kubernetes",
    "namespace": "envs",
    "api_base": "https://k8s.test",
    "token": "tok",
    "verify_tls": True,
}


def _provider(handler) -> KubernetesExecEnvProvider:  # noqa: ANN001
    return KubernetesExecEnvProvider(_ENGINE, transport=httpx.MockTransport(handler))


async def test_run_script_happy_path() -> None:
    state = {"created": None}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        path = request.url.path
        if path == "/apis/batch/v1/namespaces/envs/jobs" and request.method == "POST":
            body = json.loads(request.content)
            state["created"] = body
            command = body["spec"]["template"]["spec"]["containers"][0]["command"]
            # Prefixed by exec_env.shell's PATH prelude; the requested command is the tail.
            assert command[:2] == ["sh", "-c"]
            assert command[2].endswith("echo hi")
            return httpx.Response(201, json={})
        if path == "/api/v1/namespaces/envs/pods":
            job_name = state["created"]["metadata"]["name"]
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"name": f"{job_name}-pod"},
                            "status": {
                                "phase": "Succeeded",
                                "containerStatuses": [{"state": {"terminated": {"exitCode": 0}}}],
                            },
                        }
                    ]
                },
            )
        if path.endswith("/log"):
            return httpx.Response(200, text="hi\nPYR_TEST_RC=0")
        raise AssertionError(path)

    result = await _provider(handler).run_script("pyr-env-abc-repo", "img:1", "echo hi")
    assert result.exit_code == 0
    assert "PYR_TEST_RC=0" in result.output
    labels = state["created"]["metadata"]["labels"]
    assert labels["pyrrhula.dev/exec-env"] == "pyr-env-abc-repo"


async def test_run_script_failure_exit_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/jobs") and request.method == "POST":
            return httpx.Response(201, json={})
        if path == "/api/v1/namespaces/envs/pods":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"name": "p"},
                            "status": {
                                "phase": "Failed",
                                "containerStatuses": [{"state": {"terminated": {"exitCode": 7}}}],
                            },
                        }
                    ]
                },
            )
        if path.endswith("/log"):
            return httpx.Response(200, text="boom")
        raise AssertionError(path)

    result = await _provider(handler).run_script("n", "img", "false")
    assert result.exit_code == 7 and result.output == "boom"


async def test_teardown_matching_by_label_prefix() -> None:
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/jobs") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {
                                "name": "a1",
                                "labels": {"pyrrhula.dev/exec-env": "pyr-env-abc-x"},
                            }
                        },
                        {
                            "metadata": {
                                "name": "b1",
                                "labels": {"pyrrhula.dev/exec-env": "pyr-env-zzz-y"},
                            }
                        },
                    ]
                },
            )
        if request.method == "DELETE":
            deleted.append(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={})
        raise AssertionError(path)

    removed = await _provider(handler).teardown_matching("pyr-env-abc-")
    assert removed == 1 and deleted == ["a1"]


async def test_one_shot_contract() -> None:
    provider = _provider(lambda r: httpx.Response(500))
    with pytest.raises(ExecEnvUnavailableError):
        await provider.provision("n", "img", binds=[], setup_cmds=[])
    with pytest.raises(ExecEnvUnavailableError):
        await provider.exec("n", "true")

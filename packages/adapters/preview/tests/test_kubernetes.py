"""Kubernetes preview adapter against ``httpx.MockTransport``. No cluster.

The load-bearing claim under test: a preview is a **Job whose container never exits**,
because the runner ServiceAccount can create Jobs and pods-get/list and nothing else.
If someone "improves" this into a Deployment + Service it will fail in production with a
403, so the shape is asserted here.
"""

from __future__ import annotations

import json

import httpx
import pytest

from adapters.preview.kubernetes import KubernetesPreviewProvider
from core.ports.preview import PreviewUnavailableError

_ENGINE = {
    "key": "k8s",
    "kind": "kubernetes",
    "namespace": "envs",
    "api_base": "https://k8s.test",
    "token": "tok",
    "verify_tls": True,
}


def _provider(handler) -> KubernetesPreviewProvider:  # noqa: ANN001
    return KubernetesPreviewProvider(_ENGINE, transport=httpx.MockTransport(handler))


async def test_start_creates_a_job_and_returns_the_pod_ip() -> None:
    state: dict = {"created": None}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        path = request.url.path
        if path == "/apis/batch/v1/namespaces/envs/jobs" and request.method == "POST":
            state["created"] = json.loads(request.content)
            return httpx.Response(201, json={})
        if path == "/apis/batch/v1/namespaces/envs/jobs":
            return httpx.Response(200, json={"items": []})
        if path == "/api/v1/namespaces/envs/pods":
            job = state["created"]["metadata"]["name"]
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"name": f"{job}-pod"},
                            "status": {"phase": "Running", "podIP": "10.42.7.9"},
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected {request.method} {path}")

    handle = await _provider(handler).start(
        "pyr-prev-abcd1234",
        "python:3.12-slim",
        "serve",
        env={"PYR_ARTIFACT_TOKEN": "tok"},
        port=8080,
        ttl_seconds=3600,
    )
    assert handle.internal_url == "http://10.42.7.9:8080"
    assert handle.ref == "pyr-prev-abcd1234"

    created = state["created"]
    # A Job -- not a Deployment, and no Service alongside it (see module docstring).
    assert created["kind"] == "Job"
    assert created["apiVersion"] == "batch/v1"
    container = created["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["sh", "-lc", "serve"]
    assert container["ports"] == [{"containerPort": 8080}]
    # The token rides the environment, never the command line.
    assert {"name": "PYR_ARTIFACT_TOKEN", "value": "tok"} in container["env"]
    assert "tok" not in container["command"][2]


async def test_ttl_becomes_a_cluster_enforced_deadline() -> None:
    """The cluster must expire a preview even if the worker never runs again."""
    state: dict = {"created": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/apis/batch/v1/namespaces/envs/jobs" and request.method == "POST":
            state["created"] = json.loads(request.content)
            return httpx.Response(201, json={})
        if path == "/apis/batch/v1/namespaces/envs/jobs":
            return httpx.Response(200, json={"items": []})
        if path == "/api/v1/namespaces/envs/pods":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"name": "p"},
                            "status": {"phase": "Running", "podIP": "10.0.0.1"},
                        }
                    ]
                },
            )
        raise AssertionError(path)

    await _provider(handler).start(
        "pyr-prev-x", "img", "serve", env={}, port=8080, ttl_seconds=1800
    )
    assert state["created"]["spec"]["activeDeadlineSeconds"] == 1800
    assert state["created"]["spec"]["ttlSecondsAfterFinished"] == 300


async def test_start_replaces_a_previous_preview_of_the_same_name() -> None:
    """Reuse would silently keep serving the *old* build, which is worse than a restart."""
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/apis/batch/v1/namespaces/envs/jobs" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {
                                "name": "old-job",
                                "labels": {"pyrrhula.dev/preview": "pyr-prev-x"},
                            },
                        }
                    ]
                },
            )
        if request.method == "DELETE":
            deleted.append(path)
            return httpx.Response(200, json={})
        if path == "/apis/batch/v1/namespaces/envs/jobs":
            return httpx.Response(201, json={})
        if path == "/api/v1/namespaces/envs/pods":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"name": "p"},
                            "status": {"phase": "Running", "podIP": "10.0.0.2"},
                        }
                    ]
                },
            )
        raise AssertionError(path)

    await _provider(handler).start("pyr-prev-x", "img", "serve", env={}, port=8080)
    assert deleted == ["/apis/batch/v1/namespaces/envs/jobs/old-job"]


async def test_failed_pod_surfaces_rather_than_hanging() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/apis/batch/v1/namespaces/envs/jobs" and request.method == "POST":
            return httpx.Response(201, json={})
        if path == "/apis/batch/v1/namespaces/envs/jobs":
            return httpx.Response(200, json={"items": []})
        if path == "/api/v1/namespaces/envs/pods":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"name": "p"},
                            "status": {"phase": "Failed", "reason": "ImagePullBackOff"},
                        }
                    ]
                },
            )
        raise AssertionError(path)

    with pytest.raises(PreviewUnavailableError, match="ImagePullBackOff"):
        await _provider(handler).start("pyr-prev-x", "bad-image", "serve", env={}, port=8080)


async def test_status_reports_running_and_missing() -> None:
    def running(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"status": {"phase": "Running"}}]})

    def gone(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    assert await _provider(running).status("pyr-prev-x") == "running"
    assert await _provider(gone).status("pyr-prev-x") == "missing"


async def test_unreachable_api_is_graceful_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("")

    # teardown/status degrade; start is the one that must say why.
    assert await _provider(handler).teardown_matching("pyr-prev-") == 0
    assert await _provider(handler).status("pyr-prev-x") == "missing"
    with pytest.raises(PreviewUnavailableError, match="kubernetes api unreachable"):
        await _provider(handler).start("pyr-prev-x", "img", "serve", env={}, port=8080)


_PLAIN = "pyr-prev-6a23d529"
_BRANCH = "pyr-prev-6a23d529-pyr-45dd0469-3"


def _jobs_handler(deleted: list[str]):  # noqa: ANN202
    """A namespace holding a repo's no-branch preview and one of its branch previews."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/apis/batch/v1/namespaces/envs/jobs" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {
                                "name": "plain-job",
                                "labels": {"pyrrhula.dev/preview": _PLAIN},
                            }
                        },
                        {
                            "metadata": {
                                "name": "branch-job",
                                "labels": {"pyrrhula.dev/preview": _BRANCH},
                            }
                        },
                    ]
                },
            )
        if request.method == "DELETE":
            deleted.append(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={})
        if path == "/apis/batch/v1/namespaces/envs/jobs":
            return httpx.Response(201, json={})
        if path == "/api/v1/namespaces/envs/pods":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {"name": "p"},
                            "status": {"phase": "Running", "podIP": "10.0.0.3"},
                        }
                    ]
                },
            )
        raise AssertionError(path)

    return handler


async def test_teardown_stops_only_that_preview_not_the_branches_it_prefixes() -> None:
    """``pyr-prev-<repo8>`` (no branch) is a prefix of ``pyr-prev-<repo8>-<branch>``.
    Stopping the first used to stop every branch preview of the repo."""
    deleted: list[str] = []
    await _provider(_jobs_handler(deleted)).teardown(_PLAIN)
    assert deleted == ["plain-job"]


async def test_start_replaces_only_its_own_name() -> None:
    deleted: list[str] = []
    await _provider(_jobs_handler(deleted)).start(_PLAIN, "img", "serve", env={}, port=8080)
    assert deleted == ["plain-job"]


async def test_teardown_matching_still_sweeps_by_prefix() -> None:
    """Session-wide cleanup keeps its prefix semantics."""
    deleted: list[str] = []
    assert await _provider(_jobs_handler(deleted)).teardown_matching(_PLAIN) == 2
    assert sorted(deleted) == ["branch-job", "plain-job"]

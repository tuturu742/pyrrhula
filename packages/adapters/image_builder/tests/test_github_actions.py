"""The GitHub Actions builder against a mock GitHub API, and the reference workflow's
injection discipline."""

from __future__ import annotations

import base64
import json
import pathlib
import re

import httpx
import pytest

from adapters.image_builder.github_actions import (
    GitHubActionsConfig,
    GitHubActionsImageBuilder,
    run_name,
)
from core.ports.image_builder import BuilderError, BuildSpec

TOKEN = "github_pat_SENTINEL_" + "x" * 30
BUILD_ID = "4f0c1d2e-0000-4000-8000-0123456789ab"
_SPEC = BuildSpec(
    build_id=BUILD_ID,
    dockerfile="FROM debian:bookworm\nRUN apt-get install -y git\n",
    target_ref="ghcr.io/acme/pyrrhula/tabc/godot:abc-4f0c1d2e",
)
_CONFIG = GitHubActionsConfig(owner="acme", repo="builds", workflow="pyrrhula-image-build.yml")
WORKFLOW = pathlib.Path(__file__).resolve().parents[4] / "docs" / "builders" / "github-actions.yml"


class GitHub:
    def __init__(self, *, run_details: bool = True, run: dict[str, object] | None = None) -> None:
        self.run_details = run_details
        self.run = run or {"id": 991, "status": "in_progress", "html_url": "https://gh/run/991"}
        self.dispatched: list[dict[str, object]] = []
        self.cancelled: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["x-github-api-version"]
        path = request.url.path
        if path.endswith("/dispatches"):
            self.dispatched.append(json.loads(request.content))
            if self.run_details:
                return httpx.Response(
                    200,
                    json={"workflow_run_id": 991, "run_url": "x", "html_url": "https://gh/run/991"},
                )
            return httpx.Response(204)
        if path.endswith("/runs") and "workflows" in path:
            return httpx.Response(
                200, json={"workflow_runs": [{**self.run, "display_title": run_name(BUILD_ID)}]}
            )
        if path.endswith("/cancel"):
            self.cancelled.append(path)
            return httpx.Response(202)
        if "/actions/runs/" in path:
            return httpx.Response(200, json=self.run)
        if path.endswith("pyrrhula-image-build.yml"):
            return httpx.Response(200, json={"state": "active"})
        return httpx.Response(404, json={"message": f"Not Found (token {TOKEN})"})


def _builder(github: GitHub) -> GitHubActionsImageBuilder:
    return GitHubActionsImageBuilder(
        _CONFIG, token=TOKEN, transport=httpx.MockTransport(github), guard=False
    )


async def test_dispatch_sends_inert_inputs_and_gets_the_run_id() -> None:
    github = GitHub()
    submitted = await _builder(github).submit(_SPEC)
    assert submitted.external_ref == "991"
    body = github.dispatched[0]
    assert body["return_run_details"] is True and body["ref"] == "main"
    inputs = body["inputs"]
    assert isinstance(inputs, dict)
    assert set(inputs) == {"build_id", "target_ref", "dockerfile_b64"}
    assert base64.b64decode(inputs["dockerfile_b64"]).decode() == _SPEC.dockerfile


async def test_a_server_without_run_details_is_followed_by_run_name() -> None:
    github = GitHub(run_details=False)
    builder = _builder(github)
    submitted = await builder.submit(_SPEC)
    assert submitted.external_ref == f"pending-{BUILD_ID}"
    assert (await builder.poll(submitted.external_ref)).state == "building"


@pytest.mark.parametrize(
    ("status", "conclusion", "state"),
    [
        ("queued", None, "queued"),
        ("in_progress", None, "building"),
        ("completed", "success", "succeeded"),
        ("completed", "cancelled", "cancelled"),
        ("completed", "failure", "failed"),
        ("completed", "timed_out", "failed"),
    ],
)
async def test_run_states_map(status: str, conclusion: str | None, state: str) -> None:
    github = GitHub(run={"id": 991, "status": status, "conclusion": conclusion})
    assert (await _builder(github).poll("991")).state == state


async def test_unsafe_inputs_are_never_sent() -> None:
    github = GitHub()
    bad = BuildSpec(build_id=BUILD_ID, dockerfile="FROM x:1", target_ref="ghcr.io/a:b;rm -rf /")
    with pytest.raises(BuilderError, match="charset"):
        await _builder(github).submit(bad)
    assert github.dispatched == []
    with pytest.raises(BuilderError, match="invalid"):
        await _builder(github).poll("991/../../secrets")


async def test_cancel_and_probe() -> None:
    github = GitHub()
    builder = _builder(github)
    await builder.cancel("991")
    assert github.cancelled == ["/repos/acme/builds/actions/runs/991/cancel"]
    assert (await builder.probe()).ok


async def test_errors_never_carry_the_token() -> None:
    builder = GitHubActionsImageBuilder(
        GitHubActionsConfig(owner="acme", repo="builds", workflow="missing.yml"),
        token=TOKEN,
        transport=httpx.MockTransport(GitHub()),
        guard=False,
    )
    probe = await builder.probe()
    assert not probe.ok and TOKEN not in probe.detail


def test_the_reference_workflow_never_interpolates_inputs_into_shell() -> None:
    """``${{ inputs.x }}`` inside ``run:`` is the classic Actions injection: the value is
    pasted into the script before the shell parses it."""
    text = WORKFLOW.read_text()
    in_run = False
    offenders = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.match(r"^(- )?run: *\|?", stripped):
            in_run = True
            if "${{" in stripped:
                offenders.append(stripped)
            continue
        if in_run and re.match(r"^(- )?[a-z_-]+:", stripped) and not line.startswith(" " * 12):
            in_run = False
        if in_run and "${{" in line:
            offenders.append(stripped)
    assert not offenders, offenders
    assert "dockerfile_b64" in text and "PYRRHULA_TARGET_PREFIX" in text

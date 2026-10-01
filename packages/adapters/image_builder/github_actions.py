"""``ImageBuilder`` over GitHub Actions: dispatch a workflow the operator owns, follow the run.

The operator keeps a repository with a workflow (``docs/builders/github-actions.yml`` is the
reference) that takes three inputs -- ``build_id``, ``target_ref``, ``dockerfile_b64`` --
and builds and pushes. Pyrrhula holds only a token that may dispatch and read that one
workflow (a fine-grained PAT with ``actions:write`` on that repository); the push
credential lives in the operator's CI secrets and never reaches Pyrrhula.

- ``submit``: ``POST …/actions/workflows/<workflow>/dispatches`` with
  ``return_run_details: true``, which answers with the run id (GitHub, Feb 2026). A
  GitHub Enterprise Server that still answers ``204`` is handled by finding the run by
  its ``run-name`` (``pyrrhula <build_id>``), which the reference workflow sets.
- ``poll``: ``GET …/actions/runs/<id>``; ``status``/``conclusion`` mapped onto the port's
  states. No digest is read back -- the tag is unique to this build, so the registry's
  answer for it is the digest, and that is what Pyrrhula checks anyway.
- ``cancel``: ``POST …/actions/runs/<id>/cancel``.

Inputs are inert: the Dockerfile is base64, ``target_ref`` and ``build_id`` are restricted
to charsets that cannot carry a command, and the reference workflow reads every input
through ``env:`` -- never ``${{ inputs.* }}`` inside a ``run:`` line, which is the classic
Actions injection.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from adapters.image_builder.redact import redact
from core.net_guard import BlockedAddressError, check_host
from core.ports.image_builder import (
    BuilderError,
    BuilderProbe,
    BuildSpec,
    Progress,
    Submitted,
)

API_VERSION = "2026-03-10"
_REF = re.compile(r"^[a-z0-9.:/_-]+:[A-Za-z0-9._-]+$")
_BUILD_ID = re.compile(r"^[0-9a-f-]{36}$")
_PENDING = "pending-"
# GitHub's own limit on one workflow_dispatch input is 65,535 characters.
_MAX_INPUT = 65_000


@dataclass(frozen=True)
class GitHubActionsConfig:
    owner: str
    repo: str
    workflow: str
    ref: str = "main"
    api_base: str = "https://api.github.com"


def run_name(build_id: str) -> str:
    return f"pyrrhula {build_id}"


@dataclass
class GitHubActionsImageBuilder:
    config: GitHubActionsConfig
    token: str = field(repr=False)
    transport: httpx.AsyncBaseTransport | None = None
    guard: bool = True
    mode: Literal["poll", "stream"] = "poll"

    def _repo_url(self) -> str:
        c = self.config
        return f"{c.api_base.rstrip('/')}/repos/{c.owner}/{c.repo}"

    async def _request(
        self, method: str, url: str, payload: dict[str, Any] | None = None
    ) -> httpx.Response:
        parsed = httpx.URL(url)
        if parsed.scheme != "https":
            raise BuilderError("the GitHub API base must be https")
        if self.guard:
            try:
                await check_host(parsed.host)
            except BlockedAddressError as exc:
                raise BuilderError(str(exc)) from exc
            except OSError as exc:
                raise BuilderError(f"cannot resolve {parsed.host}") from exc
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "Authorization": f"Bearer {self.token}",
        }
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=20.0, follow_redirects=False
            ) as client:
                resp = await client.request(method, url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise BuilderError(f"GitHub is unreachable ({type(exc).__name__})") from exc
        if resp.status_code >= 300:
            message = ""
            try:
                message = str(resp.json().get("message") or "")
            except ValueError:
                message = resp.text[:200]
            detail = redact(message, [self.token], limit=300)
            raise BuilderError(f"GitHub answered HTTP {resp.status_code}: {detail}")
        return resp

    async def submit(self, spec: BuildSpec) -> Submitted:
        if not _REF.match(spec.target_ref) or not _BUILD_ID.match(spec.build_id):
            raise BuilderError("refusing to send a target or build id outside the safe charset")
        encoded = base64.b64encode(spec.dockerfile.encode()).decode()
        if len(encoded) > _MAX_INPUT:
            raise BuilderError("the Dockerfile is too large for a workflow input")
        resp = await self._request(
            "POST",
            f"{self._repo_url()}/actions/workflows/{self.config.workflow}/dispatches",
            {
                "ref": self.config.ref,
                "inputs": {
                    "build_id": spec.build_id,
                    "target_ref": spec.target_ref,
                    "dockerfile_b64": encoded,
                },
                "return_run_details": True,
            },
        )
        if resp.status_code == 200 and resp.content:
            body = resp.json()
            run_id = body.get("workflow_run_id")
            if isinstance(run_id, int):
                return Submitted(str(run_id), str(body.get("html_url") or ""))
        # 204 from a server without run details: find it by name when polling.
        return Submitted(f"{_PENDING}{spec.build_id}", "")

    async def _find_run(self, build_id: str) -> dict[str, Any] | None:
        resp = await self._request(
            "GET",
            f"{self._repo_url()}/actions/workflows/{self.config.workflow}/runs"
            "?event=workflow_dispatch&per_page=30",
        )
        for run in resp.json().get("workflow_runs") or []:
            if run.get("display_title") == run_name(build_id) or run.get("name") == run_name(
                build_id
            ):
                return dict(run)
        return None

    async def _run(self, external_ref: str) -> dict[str, Any] | None:
        if external_ref.startswith(_PENDING):
            build_id = external_ref[len(_PENDING) :]
            if not _BUILD_ID.match(build_id):
                raise BuilderError("invalid external id")
            return await self._find_run(build_id)
        if not external_ref.isdigit():
            raise BuilderError("invalid external id")
        resp = await self._request("GET", f"{self._repo_url()}/actions/runs/{external_ref}")
        return dict(resp.json())

    async def poll(self, external_ref: str) -> Progress:
        run = await self._run(external_ref)
        if run is None:
            return Progress("queued")
        url = str(run.get("html_url") or "")
        status = str(run.get("status") or "")
        if status != "completed":
            return Progress("building" if status == "in_progress" else "queued", external_url=url)
        conclusion = str(run.get("conclusion") or "")
        if conclusion == "success":
            return Progress("succeeded", external_url=url)
        if conclusion == "cancelled":
            return Progress("cancelled", external_url=url)
        return Progress(
            "failed",
            error=f"the workflow run ended '{conclusion or 'unknown'}'; see the run for its log",
            external_url=url,
        )

    async def cancel(self, external_ref: str) -> None:
        run = await self._run(external_ref)
        if run is None or not run.get("id"):
            return
        await self._request("POST", f"{self._repo_url()}/actions/runs/{int(run['id'])}/cancel")

    async def probe(self) -> BuilderProbe:
        """The token can see the workflow and it is active. Builds nothing."""
        try:
            resp = await self._request(
                "GET", f"{self._repo_url()}/actions/workflows/{self.config.workflow}"
            )
        except BuilderError as exc:
            return BuilderProbe(False, str(exc))
        state = str(resp.json().get("state") or "")
        if state != "active":
            return BuilderProbe(False, f"the workflow is {state or 'not active'}")
        return BuilderProbe(True, "workflow found and active")

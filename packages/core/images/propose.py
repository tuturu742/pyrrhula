"""Propose a Dockerfile from a repository -- a draft for a person to read, never a build.

The model is shown what a toolchain image needs to know: the repository's file list, its
manifests (``package.json``, ``pyproject.toml``, ``project.godot``, …), the repo's test
and setup commands, and the rules the validator enforces. It answers with a Dockerfile and
a short rationale. Then the platform does what it does with any Dockerfile: validates it,
holds every ``FROM`` to the namespace and allowlist, and shows the result.

**Nothing here writes.** The draft goes back to the editor unsaved; a person saves it and
presses Build. The harness is not the model's to install either -- it is appended by code
from the operator's setup commands (``core.images.builds.harness_layer``), so the prompt
says to leave it out and any line that tries is reported.

Metered like the other draft-and-approve proposals (``core.agents.editing``): one
``usage_record`` at ``purpose="rewrite"``, the tenant's egress policy on the request, and
the usage limits checked first.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from core.agents.models import Agent
from core.audit.models import UsageRecordRow
from core.ports.model_provider import GenerationRequest, ModelProvider
from core.tenancy.egress import load_egress_policy
from core.tenancy.scope import tenant_scope

_PURPOSE = "rewrite"
# Files that say what a repository needs to build and test. Their contents go to the
# model; everything else contributes its path only.
MANIFESTS = (
    "package.json",
    ".nvmrc",
    ".node-version",
    ".tool-versions",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.cfg",
    "Pipfile",
    "go.mod",
    "Cargo.toml",
    "Gemfile",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "project.godot",
    "composer.json",
    "mix.exs",
    "Makefile",
    "pyrrhula-build.json",
    ".github/workflows/ci.yml",
)
_MAX_PATHS = 400
_MAX_MANIFEST_CHARS = 4000

_SYSTEM = """You write a Dockerfile for a *toolchain image*: the tools a coding agent needs to \
build and test one repository. The repository itself is cloned into the container later; \
the image never contains it.

Rules the platform enforces -- a Dockerfile that breaks one is refused:
- the first instruction is FROM (ARG may come before it); every FROM names a tag or digest
- no ADD; no COPY except COPY --from another stage (there is no build context)
- no BuildKit-only syntax: no RUN --mount/--network/--security, no heredocs, no # syntax=
- no ONBUILD, VOLUME or LABEL pyrrhula.*
- stay root (no USER), no ENTRYPOINT; never put a secret in the file
- git must be installed; prefer official images from docker.io/library
- pin tool versions the repository pins (an .nvmrc, a Godot version in project.godot, …)
- do NOT install the coding harness; the platform appends that itself
- keep it small: one RUN per concern, clean apt lists

Return the Dockerfile and two or three sentences on why each tool is there."""


class _Proposal(BaseModel):
    dockerfile: str
    rationale: str


@dataclass(frozen=True)
class DockerfileProposal:
    dockerfile: str
    rationale: str
    errors: list[str]
    warnings: list[str]


def repository_context(
    tree: dict[str, str], *, test_cmd: str | None, setup_cmds: list[str], manifest: dict[str, Any]
) -> str:
    """What the model is shown about the repository. Pure, so it can be tested."""
    paths = sorted(tree)[:_MAX_PATHS]
    parts = ["Files:\n" + "\n".join(paths)]
    for name in MANIFESTS:
        content = tree.get(name)
        if content:
            parts.append(f"--- {name} ---\n{content[:_MAX_MANIFEST_CHARS]}")
    if test_cmd:
        parts.append(f"The repository's test command: {test_cmd}")
    if setup_cmds:
        parts.append("Setup commands it runs today:\n" + "\n".join(setup_cmds))
    if manifest:
        parts.append(f"pyrrhula-build.json says: {manifest}")
    return "\n\n".join(parts)


def _strip_fences(text: str) -> str:
    match = re.search(r"```(?:dockerfile|Dockerfile)?\s*\n(.*?)```", text, re.DOTALL)
    return (match.group(1) if match else text).strip() + "\n"


async def propose_dockerfile(
    tenant_id: uuid.UUID,
    *,
    context: str,
    harness_spec: dict[str, Any] | None,
    agent: Agent,
    provider: ModelProvider,
    api_key: str | None,
    principal_id: uuid.UUID | None,
    workspace_id: uuid.UUID | None = None,
) -> DockerfileProposal:
    from core.images.builds import check_dockerfile
    from core.usage_limits import ensure_within_limits

    await ensure_within_limits(tenant_id, agent_id=agent.id, principal_id=principal_id)
    harness_note = ""
    if harness_spec:
        setup = [str(c) for c in harness_spec.get("setup_cmds") or []]
        harness_note = (
            "\n\nThe platform will append these lines after your Dockerfile, so the image "
            "must be able to run them:\n" + "\n".join(f"RUN {c}" for c in setup)
        )
    model_string = f"{agent.provider}/{agent.model}"
    request = GenerationRequest(
        egress_policy=await load_egress_policy(tenant_id),
        model=model_string,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": context + harness_note},
        ],
        purpose=_PURPOSE,
        max_tokens=1500,
        api_base=agent.api_base,
        params=dict(agent.params or {}),
        api_key=api_key,
    )
    started = time.monotonic()
    result = await provider.generate_structured(request, _Proposal)
    latency_ms = int((time.monotonic() - started) * 1000)
    prompt_tokens = sum(
        provider.count_tokens(str(m.get("content") or ""), model_string) for m in request.messages
    )
    completion_tokens = provider.count_tokens(result.dockerfile + result.rationale, model_string)
    async with tenant_scope(tenant_id) as session:
        session.add(
            UsageRecordRow(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                agent_id=agent.id,
                provider=agent.provider,
                model=agent.model,
                purpose=_PURPOSE,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
            )
        )

    dockerfile = _strip_fences(result.dockerfile)
    checked = await check_dockerfile(tenant_id, dockerfile)
    warnings = list(checked["warnings"])
    if harness_spec:
        for cmd in harness_spec.get("setup_cmds") or []:
            if str(cmd) in dockerfile:
                warnings.append(
                    "the draft installs the harness itself; remove that line -- the platform "
                    "adds it, pinned to the version it runs"
                )
        if any(str(c).startswith("npm ") for c in harness_spec.get("setup_cmds") or []) and not (
            re.search(r"(?im)^FROM\s+\S*node", dockerfile) or re.search(r"\bnodejs\b", dockerfile)
        ):
            warnings.append("the harness installs with npm, and this image has no Node")
    return DockerfileProposal(
        dockerfile=dockerfile,
        rationale=result.rationale.strip()[:2000],
        errors=list(checked["errors"]),
        warnings=warnings,
    )

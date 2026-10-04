"""An organization's images: import a published one, watch it be checked, pick which
verified build its runtime runs.

Reads need ``repo:manage`` (the people who choose a repo's runtime); changes need
``manage_tenant``, because an image is what every delegation in the organization may end
up running. Every change is audited in ``core.images.service``. Nothing here is reachable
from model-facing tools -- an architecture test holds that.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.authz import require_tenant_permission
from api.job_queue_factory import get_job_queue
from api.middleware.auth import get_request_context
from core.images.service import (
    ImageError,
    ImageNotFoundError,
    cancel_build,
    import_image,
    list_builds,
    list_images,
    promote_build,
    recheck_image,
    remove_image,
)
from core.tenancy.context import RequestContext

router = APIRouter(prefix="/images", tags=["images"])


class HarnessClaim(BaseModel):
    key: str
    version: str = ""


class ImportImageRequest(BaseModel):
    name: str
    image: str
    dockerfile: str = ""
    harness_claim: HarnessClaim | None = None


class BuildOut(BaseModel):
    id: str
    origin: str
    status: str
    target_ref: str
    digest: str
    pinned_ref: str
    registry_key: str | None
    builder_key: str | None
    external_url: str
    harness_claim: dict[str, Any]
    baked_harness: dict[str, Any]
    smoke: str
    log_tail: str = ""
    error: str
    cancel_requested: bool
    created_at: datetime
    finished_at: datetime | None
    current: bool = False
    # A build request whose recipe was already built here: the stored digest is reused.
    reused: bool = False


class ImageOut(BaseModel):
    id: str
    name: str
    origin: str
    harness_key: str
    dockerfile: str
    current: BuildOut | None
    latest: BuildOut | None
    updated_at: datetime


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, ImageNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.get("")
async def list_images_endpoint(
    ctx: RequestContext = Depends(get_request_context),
) -> list[ImageOut]:
    await require_tenant_permission(ctx, "repo:manage")
    return [ImageOut.model_validate(i) for i in await list_images(ctx.tenant_id)]


@router.post("/import", status_code=202)
async def import_image_endpoint(
    body: ImportImageRequest, ctx: RequestContext = Depends(get_request_context)
) -> BuildOut:
    """Start using a published image by its exact digest. Answers at once; the check --
    the registry's digest, then a smoke test on this organization's engine -- runs in the
    worker, and the image becomes a runtime when it passes."""
    await require_tenant_permission(ctx, "manage_tenant")
    try:
        build = await import_image(
            ctx.tenant_id,
            name=body.name,
            image=body.image,
            dockerfile=body.dockerfile,
            harness_claim=body.harness_claim.model_dump() if body.harness_claim else None,
            requested_by=ctx.principal_id,
            queue=get_job_queue(),
        )
    except (ImageError, ImageNotFoundError) as exc:
        raise _http(exc) from exc
    return BuildOut.model_validate(build)


class BuilderChoice(BaseModel):
    key: str
    label: str
    kind: str
    registry_key: str


class DockerfileBody(BaseModel):
    dockerfile: str


class DockerfileCheck(BaseModel):
    errors: list[str]
    warnings: list[str]


class SaveDefinitionRequest(BaseModel):
    dockerfile: str
    harness_key: str = ""


class BuildRequest(BaseModel):
    builder_key: str
    # Same recipe, new bytes: base images move and RUN steps are not reproducible.
    rebuild: bool = False


class TemplateOut(BaseModel):
    key: str
    label: str
    description: str
    dockerfile: str
    harness_key: str


class ProposeRequest(BaseModel):
    repo_id: uuid.UUID
    # The model connection to draft with -- one of the organization's own.
    agent_id: uuid.UUID
    harness_key: str = ""


class ProposalOut(BaseModel):
    dockerfile: str
    rationale: str
    errors: list[str]
    warnings: list[str]


@router.get("/templates")
async def list_templates_endpoint(
    ctx: RequestContext = Depends(get_request_context),
) -> list[TemplateOut]:
    from core.images.templates import list_templates

    await require_tenant_permission(ctx, "repo:manage")
    return [TemplateOut.model_validate(t) for t in list_templates()]


@router.post("/propose")
async def propose_endpoint(
    body: ProposeRequest, ctx: RequestContext = Depends(get_request_context)
) -> ProposalOut:
    """A draft Dockerfile for a repository, from its files and manifests. Writes nothing:
    the draft goes back to the editor, and a person saves and builds it."""
    from adapters.mcp.git_store import GitStore, GitStoreError, default_git_root
    from api.encryptor_factory import get_encryptor
    from api.model_provider_factory import get_model_provider
    from core.agents.authoring import get_agent, resolve_connection_api_key
    from core.harness.registry import get_harness
    from core.images.propose import MANIFESTS, propose_dockerfile, repository_context
    from core.repos.build_recipe import BuildRecipeError, read_repo_manifest
    from core.repos.service import get_repo, store_key
    from core.usage_limits import UsageLimitExceededError

    await require_tenant_permission(ctx, "manage_tenant")
    repo = await get_repo(ctx.tenant_id, body.repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="no such repository")
    agent = await get_agent(ctx.tenant_id, body.agent_id)
    if agent is None or not agent.provider:
        raise HTTPException(status_code=422, detail="choose a configured model connection")
    spec = None
    if body.harness_key:
        found = await get_harness(ctx.tenant_id, body.harness_key)
        if found is None:
            raise HTTPException(status_code=422, detail=f"no harness {body.harness_key!r}")
        spec = {"key": body.harness_key, **found}

    skey = store_key(ctx.tenant_id, repo.key)
    branch = repo.default_branch or "main"
    try:
        tree = await GitStore(default_git_root()).read_tree(
            skey, ref=branch, max_files=len(MANIFESTS), prefer=MANIFESTS, max_file_bytes=8000
        )
    except GitStoreError as exc:
        raise HTTPException(status_code=422, detail=f"cannot read the repository: {exc}") from exc
    try:
        manifest = await read_repo_manifest(skey, ref=branch)
    except BuildRecipeError:
        manifest = {}
    context = repository_context(
        tree,
        test_cmd=repo.test_cmd,
        setup_cmds=list(repo.setup_cmds or []),
        manifest=manifest,
    )
    try:
        proposal = await propose_dockerfile(
            ctx.tenant_id,
            context=context,
            harness_spec=spec,
            agent=agent,
            provider=get_model_provider(agent.provider),
            api_key=await resolve_connection_api_key(
                ctx.tenant_id, agent.credential_ref, encryptor=get_encryptor()
            ),
            principal_id=ctx.principal_id,
        )
    except UsageLimitExceededError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return ProposalOut(
        dockerfile=proposal.dockerfile,
        rationale=proposal.rationale,
        errors=proposal.errors,
        warnings=proposal.warnings,
    )


@router.get("/builders")
async def list_available_builders_endpoint(
    ctx: RequestContext = Depends(get_request_context),
) -> list[BuilderChoice]:
    """The builders the operator made available to this organization. Empty means
    Build is unavailable here -- imports still work."""
    from core.images.builders import builders_for_tenant

    await require_tenant_permission(ctx, "repo:manage")
    return [
        BuilderChoice(key=b.key, label=b.label or b.key, kind=b.kind, registry_key=b.registry_key)
        for b in await builders_for_tenant(ctx.tenant_id)
    ]


@router.post("/validate")
async def validate_dockerfile_endpoint(
    body: DockerfileBody, ctx: RequestContext = Depends(get_request_context)
) -> DockerfileCheck:
    from core.images.builds import check_dockerfile

    await require_tenant_permission(ctx, "repo:manage")
    return DockerfileCheck(**await check_dockerfile(ctx.tenant_id, body.dockerfile))


@router.put("/{name}")
async def save_definition_endpoint(
    name: str, body: SaveDefinitionRequest, ctx: RequestContext = Depends(get_request_context)
) -> DockerfileCheck:
    """Save an image this organization builds. Builds nothing; answers with what the
    validator thinks of it, so the editor can show it next to the save."""
    from core.images.builds import check_dockerfile, save_definition

    await require_tenant_permission(ctx, "manage_tenant")
    try:
        await save_definition(
            ctx.tenant_id,
            name,
            dockerfile=body.dockerfile,
            harness_key=body.harness_key,
            actor=ctx.principal_id,
        )
    except (ImageError, ImageNotFoundError) as exc:
        raise _http(exc) from exc
    return DockerfileCheck(**await check_dockerfile(ctx.tenant_id, body.dockerfile))


@router.post("/{name}/build", status_code=202)
async def build_endpoint(
    name: str, body: BuildRequest, ctx: RequestContext = Depends(get_request_context)
) -> BuildOut:
    from core.images.builds import request_build

    await require_tenant_permission(ctx, "manage_tenant")
    try:
        build = await request_build(
            ctx.tenant_id,
            name,
            builder_key=body.builder_key,
            requested_by=ctx.principal_id,
            rebuild=body.rebuild,
            queue=get_job_queue(),
        )
    except (ImageError, ImageNotFoundError) as exc:
        raise _http(exc) from exc
    return BuildOut.model_validate(build)


@router.get("/{name}/builds")
async def list_builds_endpoint(
    name: str, ctx: RequestContext = Depends(get_request_context)
) -> list[BuildOut]:
    await require_tenant_permission(ctx, "repo:manage")
    try:
        return [BuildOut.model_validate(b) for b in await list_builds(ctx.tenant_id, name)]
    except ImageNotFoundError as exc:
        raise _http(exc) from exc


@router.post("/{name}/recheck", status_code=202)
async def recheck_image_endpoint(
    name: str, ctx: RequestContext = Depends(get_request_context)
) -> BuildOut:
    await require_tenant_permission(ctx, "manage_tenant")
    try:
        build = await recheck_image(
            ctx.tenant_id, name, requested_by=ctx.principal_id, queue=get_job_queue()
        )
    except (ImageError, ImageNotFoundError) as exc:
        raise _http(exc) from exc
    return BuildOut.model_validate(build)


@router.post("/{name}/cancel")
async def cancel_build_endpoint(
    name: str, ctx: RequestContext = Depends(get_request_context)
) -> BuildOut | None:
    await require_tenant_permission(ctx, "manage_tenant")
    try:
        build = await cancel_build(ctx.tenant_id, name, actor=ctx.principal_id)
    except ImageNotFoundError as exc:
        raise _http(exc) from exc
    return BuildOut.model_validate(build) if build else None


@router.post("/{name}/builds/{build_id}/promote", status_code=204)
async def promote_build_endpoint(
    name: str, build_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> None:
    """Make an earlier verified build current again -- the rollback."""
    await require_tenant_permission(ctx, "manage_tenant")
    try:
        builds = await list_builds(ctx.tenant_id, name)
        if not any(b["id"] == str(build_id) for b in builds):
            raise ImageNotFoundError(f"image {name!r} has no build {build_id}")
        await promote_build(ctx.tenant_id, build_id, actor=ctx.principal_id)
    except (ImageError, ImageNotFoundError) as exc:
        raise _http(exc) from exc


@router.delete("/{name}", status_code=204)
async def remove_image_endpoint(
    name: str, ctx: RequestContext = Depends(get_request_context)
) -> None:
    await require_tenant_permission(ctx, "manage_tenant")
    try:
        await remove_image(ctx.tenant_id, name, actor=ctx.principal_id)
    except ImageNotFoundError as exc:
        raise _http(exc) from exc

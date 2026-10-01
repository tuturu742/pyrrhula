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
    error: str
    cancel_requested: bool
    created_at: datetime
    finished_at: datetime | None
    current: bool = False


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

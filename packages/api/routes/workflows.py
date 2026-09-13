"""Tenant-facing workflow endpoints (moddable workflows).

Listing/reading is open to any authenticated member; creating, editing, deleting and
selecting the tenant's current workflow are gated on ``workflow:manage`` (tenant-scoped,
granted to owner + admin). System templates (tenant_id NULL) are listed alongside the
tenant's own and are structurally read-only -- clone them instead.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from api.authz import require_tenant_permission
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from core.tenancy.context import RequestContext
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope
from core.workflows.models import WorkflowRow
from core.workflows.service import (
    InvalidWorkflowError,
    WorkflowNotEditableError,
    WorkflowNotFoundError,
    apply_workflow_capabilities,
    create_workflow,
    delete_workflow,
    get_tenant_workflow_key,
    get_workflow_for_tenant,
    list_workflows_for_tenant,
    set_tenant_workflow,
    update_workflow,
)

router = APIRouter(
    prefix="/workflows",
    tags=["workflows"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


class WorkflowResponse(BaseModel):
    key: str
    name: str
    is_system: bool
    overlay_key: str | None = None
    persona_type_labels: dict[str, str] = {}
    label_overrides: dict[str, str] = {}
    featured_process_keys: list[str] = []
    repo_access: bool = False


def _response(row: WorkflowRow) -> WorkflowResponse:
    caps: dict[str, Any] = dict(row.capabilities or {})
    if row.tenant_id is None:
        repo_access = any(
            "delegate_work_item" in (spec.get("enabled_tools") or [])
            for spec in caps.get("mcp_servers", [])
        )
    else:
        repo_access = bool(caps.get("repo_access"))
    return WorkflowResponse(
        key=row.key,
        name=row.name,
        is_system=row.tenant_id is None,
        overlay_key=row.overlay_key,
        persona_type_labels=dict(row.persona_type_labels or {}),
        label_overrides=dict(row.label_overrides or {}),
        featured_process_keys=list(row.featured_process_keys or []),
        repo_access=repo_access,
    )


@router.get("")
async def list_workflows_endpoint(
    ctx: RequestContext = Depends(get_request_context),
) -> list[WorkflowResponse]:
    return [_response(r) for r in await list_workflows_for_tenant(ctx.tenant_id)]


class CurrentWorkflowResponse(BaseModel):
    workflow_key: str | None = None
    workflow: WorkflowResponse | None = None


@router.get("/current")
async def get_current_workflow_endpoint(
    ctx: RequestContext = Depends(get_request_context),
) -> CurrentWorkflowResponse:
    key = await get_tenant_workflow_key(ctx.tenant_id)
    if key is None:
        return CurrentWorkflowResponse()
    row = await get_workflow_for_tenant(ctx.tenant_id, key)
    return CurrentWorkflowResponse(
        workflow_key=key, workflow=_response(row) if row is not None else None
    )


class SetCurrentWorkflowRequest(BaseModel):
    workflow_key: str | None = None


@router.put("/current")
async def set_current_workflow_endpoint(
    body: SetCurrentWorkflowRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> CurrentWorkflowResponse:
    """Select (or clear) the tenant's workflow: pins its overlay (materializing label
    overrides), then provisions its capabilities onto every workspace."""
    await require_tenant_permission(ctx, "workflow:manage")
    try:
        await set_tenant_workflow(ctx.tenant_id, body.workflow_key)
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if body.workflow_key is not None:
        async with tenant_scope(ctx.tenant_id) as session:
            workspace_ids = list(
                (
                    await session.execute(
                        select(Workspace.id).where(Workspace.tenant_id == ctx.tenant_id)
                    )
                ).scalars()
            )
        for wid in workspace_ids:
            await apply_workflow_capabilities(ctx.tenant_id, wid)
    return await get_current_workflow_endpoint(ctx)


class CreateWorkflowRequest(BaseModel):
    key: str
    name: str
    clone_from: str | None = None
    overlay_key: str | None = None
    persona_type_labels: dict[str, str] | None = None
    label_overrides: dict[str, str] | None = None
    featured_process_keys: list[str] | None = None
    repo_access: bool = False


@router.post("", status_code=201)
async def create_workflow_endpoint(
    body: CreateWorkflowRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> WorkflowResponse:
    await require_tenant_permission(ctx, "workflow:manage")
    try:
        row = await create_workflow(
            ctx.tenant_id,
            body.key,
            body.name,
            overlay_key=body.overlay_key,
            persona_type_labels=body.persona_type_labels,
            label_overrides=body.label_overrides,
            featured_process_keys=body.featured_process_keys,
            repo_access=body.repo_access,
            clone_from=body.clone_from,
            created_by=ctx.principal_id,
        )
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidWorkflowError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # unique-key collision -> a clear 409, not a 500
        if "uq_workflow" in str(exc):
            raise HTTPException(
                status_code=409, detail=f"workflow {body.key!r} already exists"
            ) from exc
        raise
    return _response(row)


class UpdateWorkflowRequest(BaseModel):
    name: str | None = None
    overlay_key: str | None = None
    clear_overlay: bool = False
    persona_type_labels: dict[str, str] | None = None
    label_overrides: dict[str, str] | None = None
    featured_process_keys: list[str] | None = None
    repo_access: bool | None = None


@router.patch("/{key}")
async def update_workflow_endpoint(
    key: str,
    body: UpdateWorkflowRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> WorkflowResponse:
    await require_tenant_permission(ctx, "workflow:manage")
    overlay_arg: str | None | object = ...
    if body.clear_overlay:
        overlay_arg = None
    elif body.overlay_key is not None:
        overlay_arg = body.overlay_key
    try:
        row = await update_workflow(
            ctx.tenant_id,
            key,
            name=body.name,
            overlay_key=overlay_arg,
            persona_type_labels=body.persona_type_labels,
            label_overrides=body.label_overrides,
            featured_process_keys=body.featured_process_keys,
            repo_access=body.repo_access,
        )
    except WorkflowNotEditableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidWorkflowError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Selected workflow edited -> re-materialize labels/capabilities immediately.
    if await get_tenant_workflow_key(ctx.tenant_id) == key:
        await set_tenant_workflow(ctx.tenant_id, key)
    return _response(row)


@router.delete("/{key}", status_code=204)
async def delete_workflow_endpoint(
    key: str, ctx: RequestContext = Depends(get_request_context)
) -> None:
    await require_tenant_permission(ctx, "workflow:manage")
    if await get_tenant_workflow_key(ctx.tenant_id) == key:
        raise HTTPException(
            status_code=409, detail="this workflow is currently selected; switch first"
        )
    try:
        await delete_workflow(ctx.tenant_id, key)
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

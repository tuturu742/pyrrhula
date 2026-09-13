"""Workflow selection, tenant authoring, and capability provisioning (#2 + moddable
workflows).

Selecting a workflow for a tenant pins its vocabulary overlay (reusing the Phase-1
tenant-default mechanism) and records the workflow key in ``tenant.settings``. Applying its
capabilities to a workspace registers the declared MCP servers on that workspace's allowlist
(``core.mcp.registry.register_server``) -- so, e.g., the software-development workflow is what
gives a workspace's personas git access, egress-controlled by the MCP allowlist (not D14).

Tenant authoring: workflows with a non-NULL ``tenant_id`` are a tenant's own (RLS-scoped,
shadowing a same-keyed global on lookup); NULL rows are read-only system templates. A
tenant-authored workflow's ``label_overrides`` are materialized into a tenant
``vocabulary_overlay`` row (``wf-<key>``) when the workflow is selected, so custom labels
flow through the existing resolve chain untouched. Tenant rows carry only the safe
``{"repo_access": bool}`` capability -- never raw MCP server configs.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy import select

from core.mcp.registry import register_server
from core.tenancy.models import Tenant, Workspace
from core.tenancy.scope import tenant_scope, unscoped_session
from core.vocabulary.service import get_overlay_by_key, set_tenant_default_overlay, upsert_overlay
from core.workflows.models import WorkflowRow

_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,62}")


class WorkflowNotFoundError(Exception):
    pass


class WorkflowNotEditableError(Exception):
    """The target is a global system template -- clone it instead of editing it."""


class InvalidWorkflowError(Exception):
    pass


async def list_workflows() -> list[WorkflowRow]:
    """Global system workflows only (unscoped session -> RLS shows just NULL-tenant rows).
    The admin console's listing; tenant-facing callers use ``list_workflows_for_tenant``."""
    async with unscoped_session() as session:
        rows = (await session.execute(select(WorkflowRow).order_by(WorkflowRow.name))).scalars()
        return list(rows)


async def list_workflows_for_tenant(tenant_id: uuid.UUID) -> list[WorkflowRow]:
    """System templates + this tenant's own (RLS makes that the visible set). Own rows
    first, then templates, each alphabetical."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(WorkflowRow).order_by(WorkflowRow.tenant_id.is_(None), WorkflowRow.name)
            )
        ).scalars()
        return list(rows)


async def get_workflow(key: str) -> WorkflowRow | None:
    """Global lookup (admin path)."""
    async with unscoped_session() as session:
        row: WorkflowRow | None = await session.scalar(
            select(WorkflowRow).where(WorkflowRow.key == key)
        )
        return row


async def get_workflow_for_tenant(tenant_id: uuid.UUID, key: str) -> WorkflowRow | None:
    """Tenant-aware lookup: the tenant's own row shadows a same-keyed system template."""
    async with tenant_scope(tenant_id) as session:
        rows = list(
            (await session.execute(select(WorkflowRow).where(WorkflowRow.key == key))).scalars()
        )
    own = next((r for r in rows if r.tenant_id == tenant_id), None)
    return own or next(iter(rows), None)


def _validate_key(key: str) -> None:
    if not _KEY_RE.fullmatch(key):
        raise InvalidWorkflowError(
            f"invalid workflow key {key!r}: lowercase letters/digits/-/_, 2..63 chars"
        )


async def create_workflow(
    tenant_id: uuid.UUID,
    key: str,
    name: str,
    *,
    overlay_key: str | None = None,
    persona_type_labels: dict[str, str] | None = None,
    label_overrides: dict[str, str] | None = None,
    featured_process_keys: list[str] | None = None,
    repo_access: bool = False,
    clone_from: str | None = None,
    created_by: uuid.UUID | None = None,
) -> WorkflowRow:
    """Create a tenant workflow, optionally seeded from a template (``clone_from`` -- any
    workflow visible to the tenant). Explicit arguments win over cloned values. The only
    capability a tenant row carries is ``repo_access`` (see module docstring)."""
    _validate_key(key)
    base: WorkflowRow | None = None
    if clone_from is not None:
        base = await get_workflow_for_tenant(tenant_id, clone_from)
        if base is None:
            raise WorkflowNotFoundError(f"no workflow {clone_from!r} to clone")

    if overlay_key is None and base is not None:
        overlay_key = base.overlay_key
    if overlay_key is not None and await get_overlay_by_key(tenant_id, overlay_key) is None:
        raise InvalidWorkflowError(f"no vocabulary overlay {overlay_key!r}")

    if persona_type_labels is None:
        persona_type_labels = dict(base.persona_type_labels) if base is not None else {}
    if label_overrides is None:
        label_overrides = dict(base.label_overrides) if base is not None else {}
    if featured_process_keys is None:
        featured_process_keys = list(base.featured_process_keys) if base is not None else []
    if clone_from is not None and base is not None and not repo_access:
        # Cloning swdev-like templates keeps their repo access as a boolean intent.
        repo_access = _grants_repo_access(base)

    async with tenant_scope(tenant_id) as session:
        row = WorkflowRow(
            tenant_id=tenant_id,
            key=key,
            name=name,
            overlay_key=overlay_key,
            persona_type_labels=persona_type_labels,
            label_overrides=label_overrides,
            featured_process_keys=featured_process_keys,
            capabilities={"repo_access": repo_access},
            created_by=created_by,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row


async def update_workflow(
    tenant_id: uuid.UUID,
    key: str,
    *,
    name: str | None = None,
    overlay_key: str | None | object = ...,
    persona_type_labels: dict[str, str] | None = None,
    label_overrides: dict[str, str] | None = None,
    featured_process_keys: list[str] | None = None,
    repo_access: bool | None = None,
) -> WorkflowRow:
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(WorkflowRow).where(WorkflowRow.tenant_id == tenant_id, WorkflowRow.key == key)
        )
        if row is None:
            template = await get_workflow_for_tenant(tenant_id, key)
            if template is not None:
                raise WorkflowNotEditableError(
                    f"{key!r} is a system template; clone it to customize"
                )
            raise WorkflowNotFoundError(f"no workflow {key!r} in this tenant")
        if name is not None:
            row.name = name
        if overlay_key is not ...:
            if (
                overlay_key is not None
                and await get_overlay_by_key(
                    tenant_id,
                    overlay_key,  # type: ignore[arg-type]
                )
                is None
            ):
                raise InvalidWorkflowError(f"no vocabulary overlay {overlay_key!r}")
            row.overlay_key = overlay_key  # type: ignore[assignment]
        if persona_type_labels is not None:
            row.persona_type_labels = dict(persona_type_labels)
        if label_overrides is not None:
            row.label_overrides = dict(label_overrides)
        if featured_process_keys is not None:
            row.featured_process_keys = list(featured_process_keys)
        if repo_access is not None:
            row.capabilities = {**dict(row.capabilities), "repo_access": repo_access}
        await session.flush()
        await session.refresh(row)
        return row


async def delete_workflow(tenant_id: uuid.UUID, key: str) -> None:
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(WorkflowRow).where(WorkflowRow.tenant_id == tenant_id, WorkflowRow.key == key)
        )
        if row is None:
            raise WorkflowNotFoundError(f"no tenant workflow {key!r} to delete")
        await session.delete(row)
        await session.flush()


async def get_tenant_workflow_key(tenant_id: uuid.UUID) -> str | None:
    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            return None
        key = tenant.settings.get("workflow_key")
        return key if isinstance(key, str) else None


def _grants_repo_access(workflow: WorkflowRow) -> bool:
    """Does this workflow give sessions repo access? Tenant rows: the ``repo_access``
    boolean. Global rows: any git-ish server in their raw capabilities (a server offering
    ``delegate_work_item``)."""
    caps = workflow.capabilities or {}
    if workflow.tenant_id is not None:
        return bool(caps.get("repo_access"))
    servers = caps.get("mcp_servers") or []
    if not isinstance(servers, list):
        return False
    return any(
        isinstance(spec, dict) and "delegate_work_item" in (spec.get("enabled_tools") or [])
        for spec in servers
    )


async def tenant_workflow_grants_repo_access(tenant_id: uuid.UUID) -> bool:
    key = await get_tenant_workflow_key(tenant_id)
    if key is None:
        return False
    workflow = await get_workflow_for_tenant(tenant_id, key)
    return workflow is not None and _grants_repo_access(workflow)


async def set_tenant_workflow(tenant_id: uuid.UUID, workflow_key: str | None) -> None:
    """Pin (or clear) a tenant's workflow. Setting one pins its overlay; a workflow with
    ``label_overrides`` first gets those materialized into a tenant overlay (``wf-<key>``,
    base overlay's labels merged under the overrides) so labels follow the workflow through
    the untouched resolve chain."""
    if workflow_key is not None:
        workflow = await get_workflow_for_tenant(tenant_id, workflow_key)
        if workflow is None:
            raise WorkflowNotFoundError(f"no workflow {workflow_key!r}")
        overlay_key = workflow.overlay_key
        if workflow.label_overrides:
            base_labels: dict[str, str] = {}
            if overlay_key is not None:
                base = await get_overlay_by_key(tenant_id, overlay_key)
                if base is not None:
                    base_labels = dict(base.labels)
            materialized_key = f"wf-{workflow.key}"
            await upsert_overlay(
                tenant_id,
                materialized_key,
                workflow.name,
                {**base_labels, **dict(workflow.label_overrides)},
            )
            overlay_key = materialized_key
    else:
        overlay_key = None

    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise ValueError(f"no tenant {tenant_id}")
        settings = dict(tenant.settings)
        if workflow_key is None:
            settings.pop("workflow_key", None)
        else:
            settings["workflow_key"] = workflow_key
        tenant.settings = settings

    # Keep the overlay pinned to the workflow's (separate transaction / its own connection).
    await set_tenant_default_overlay(tenant_id, overlay_key)

    if workflow_key is not None:
        await _load_workflow_pack(tenant_id, workflow_key)


async def _load_workflow_pack(tenant_id: uuid.UUID, workflow_key: str) -> None:
    """Materialize the workflow's pack content (schemas, processes, rule systems,
    tools, axes) into the tenant through the generic loader -- selecting a workflow is
    what makes its content exist for a tenant. Idempotent per plugin ref: a stamp in
    ``tenant.settings`` skips reloading the same content (the loader would otherwise
    version-bump schemas/definitions on every reselect). Global system templates whose
    content shipped via migrations (no plugin owner) load nothing here."""
    from core.packs.loader import load_pack
    from core.plugins.service import list_repositories, pack_dir_for_workflow

    repositories = await list_repositories()
    pack_dir = pack_dir_for_workflow(workflow_key, repositories)
    if pack_dir is None or not pack_dir.is_dir():
        return
    owner = next(r for r in repositories if workflow_key in r.workflow_keys)
    stamp = f"{owner.name}@{owner.ref}:{workflow_key}"

    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        stored = dict(tenant.settings).get("loaded_workflow_packs")
        loaded = stored if isinstance(stored, dict) else {}
        if loaded.get(workflow_key) == stamp:
            return

    async with tenant_scope(tenant_id) as session:
        workspace_id = await session.scalar(
            select(Workspace.id).where(Workspace.tenant_id == tenant_id).limit(1)
        )
    await load_pack(pack_dir, tenant_id, workspace_id)

    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        assert tenant is not None
        settings = dict(tenant.settings)
        raw_loaded = settings.get("loaded_workflow_packs")
        loaded = dict(raw_loaded) if isinstance(raw_loaded, dict) else {}
        loaded[workflow_key] = stamp
        settings["loaded_workflow_packs"] = loaded
        tenant.settings = settings


async def apply_workflow_capabilities(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> list[str]:
    """Provision the tenant workflow's declared MCP servers onto a workspace's allowlist.
    Idempotent (register_server upserts by (workspace, key)). Returns the server keys applied
    -- empty when the tenant has no workflow, or the workflow declares no raw servers
    (tenant-authored rows declare none; their ``repo_access`` is interpreted per-session at
    repo selection time instead)."""
    from core.mcp.registry import apply_tenant_capabilities

    applied: list[str] = []
    workflow_key = await get_tenant_workflow_key(tenant_id)
    workflow = (
        await get_workflow_for_tenant(tenant_id, workflow_key) if workflow_key is not None else None
    )
    if workflow is not None:
        raw_servers = workflow.capabilities.get("mcp_servers") if workflow.capabilities else None
        servers = raw_servers if isinstance(raw_servers, list) else []
        for spec in servers:
            await register_server(
                tenant_id,
                workspace_id,
                spec["key"],
                # Manifests are tenant-agnostic content; in-process servers (e.g. the
                # resolution preset) address the tenant through this placeholder.
                str(spec["url"]).replace("{tenant_id}", str(tenant_id)),
                enabled_tools=spec.get("enabled_tools", []),
                effectful_tools=spec.get("effectful_tools", []),
            )
            applied.append(spec["key"])
    # Admin-attached tenant grants apply on top (and regardless of whether a workflow
    # is pinned at all) -- last writer wins a key collision, so the operator's explicit
    # grant beats a pack default of the same key.
    applied.extend(await apply_tenant_capabilities(tenant_id, workspace_id))
    return applied

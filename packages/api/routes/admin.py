"""Platform-admin routes, mounted on the MAIN api under ``/admin``.

Two ways in, checked in this order by ``require_platform_admin``:

1. the legacy shared bearer token (``PYRRHULA_ADMIN_TOKEN``) -- kept for scripts, CI
   and bootstrap, and for the deprecated standalone admin app (which now just mounts
   this same router);
2. a normal user JWT belonging to a platform admin -- an owner/admin of the reserved
   admin tenant (the "log in with organization 'admin'" path), or, on a single-tenant
   deployment, the sole organization's own owner. ``api.auth.platform_admin`` is the
   single place that decides, so the gate and what ``/me`` reports cannot disagree.

Deactivation is a soft UPDATE; truly deleting a tenant stays the ``core.tenancy.purge``
CLI's job, never a button here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import APIRouter, Cookie, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr

from adapters.identity.local.argon2_provider import LocalArgon2IdentityProvider
from api.auth.platform_admin import is_platform_admin
from api.middleware.auth import get_request_context
from core.config import get_settings
from core.mcp.registry import is_external_mcp_url
from core.tenancy.admin import ADMIN_TENANT_ID
from core.tenancy.provisioning import (
    TenantExistsError,
    create_tenant,
    create_tenant_user,
    delete_principal,
    list_tenant_users,
    list_tenant_workspace_ids,
    list_tenants,
    set_principal_disabled,
    set_tenant_deactivated,
)
from core.vocabulary.service import list_overlays, set_tenant_default_overlay
from core.workflows.service import (
    WorkflowNotFoundError,
    apply_workflow_capabilities,
    list_workflows,
    set_tenant_workflow,
)

_ROLES = {"owner", "admin", "editor", "participant", "viewer"}
_identity_provider = LocalArgon2IdentityProvider()


async def require_platform_admin(
    authorization: str | None = Header(default=None),
    pyrrhula_session: str | None = Cookie(default=None),
) -> None:
    """Admit the legacy ops token OR a platform admin.

    "Platform admin" is not always an admin-tenant membership: on a single-tenant
    deployment the sole organization's owner is one, because there is no one else it
    could be. See ``api.auth.platform_admin``."""
    expected = get_settings().admin_token
    scheme, _, value = (authorization or "").partition(" ")
    if expected and scheme.lower() == "bearer" and value == expected:
        return

    # Not the ops token -> the normal JWT path (raises 401 when no token at all).
    ctx = await get_request_context(authorization, pyrrhula_session, None)
    if not await is_platform_admin(ctx.tenant_id, ctx.principal_id):
        raise HTTPException(status_code=403, detail="platform admin required")


# A whole HF cache for the default models is ~4GB; the ceiling is generous but finite
# so a mistaken upload cannot fill the volume both services read from.
_MAX_CACHE_UPLOAD_BYTES = 12 * 1024 * 1024 * 1024

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_platform_admin)])


async def _audit_admin(
    tenant_id: uuid.UUID, action: str, resource_type: str, detail: dict[str, object]
) -> None:
    """Platform-admin changes to a tenant's configuration are auditable events in THAT
    tenant's chain (it is their record of what was changed on their behalf). The acting
    principal is the admin tenant's own principal when a JWT was used; the shared ops
    token has no principal, so those rows attribute to the admin tenant id itself."""
    try:
        from core.audit.service import AuditService

        await AuditService().append(
            tenant_id=tenant_id,
            actor_principal_id=ADMIN_TENANT_ID,
            action=action,
            resource_type=resource_type,
            resource_id=tenant_id,
            query=detail,
        )
    except Exception as exc:  # noqa: BLE001 -- never block an admin action on audit
        import structlog

        structlog.get_logger().warning("audit.admin_failed", action=action, error=str(exc)[:200])


# ── tenants ──────────────────────────────────────────────────────────────────────────
class TenantOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    deactivated: bool
    member_count: int
    agent_count: int
    overlay_key: str | None
    workflow_key: str | None


class CreateTenantRequest(BaseModel):
    slug: str
    name: str
    owner_email: EmailStr | None = None
    owner_password: str | None = None
    owner_display_name: str = "Owner"


@router.get("/tenants")
async def list_tenants_endpoint() -> list[TenantOut]:
    return [
        TenantOut(
            id=t.id,
            slug=t.slug,
            name=t.name,
            deactivated=t.deactivated_at is not None,
            member_count=t.member_count,
            agent_count=t.agent_count,
            overlay_key=t.default_overlay_key,
            workflow_key=t.workflow_key,
        )
        for t in await list_tenants()
    ]


# ── workflow (overlay + persona labels + capabilities like git) ──────────────────────
class WorkflowOut(BaseModel):
    key: str
    name: str
    overlay_key: str | None
    persona_type_labels: dict[str, str]
    capabilities: dict[str, object]


class SetWorkflowRequest(BaseModel):
    workflow_key: str | None = None


@router.get("/workflows")
async def list_workflows_endpoint() -> list[WorkflowOut]:
    return [
        WorkflowOut(
            key=w.key,
            name=w.name,
            overlay_key=w.overlay_key,
            persona_type_labels=w.persona_type_labels,
            capabilities=w.capabilities,
        )
        for w in await list_workflows()
    ]


@router.post("/tenants/{tenant_id}/workflow")
async def set_workflow_endpoint(
    tenant_id: uuid.UUID, body: SetWorkflowRequest
) -> dict[str, object]:
    """Pin a tenant's workflow: applies its overlay + persona-type labels, and provisions its
    capabilities (e.g. git via MCP) onto every workspace in the tenant."""
    try:
        await set_tenant_workflow(tenant_id, body.workflow_key)
    except WorkflowNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    applied: list[str] = []
    if body.workflow_key is not None:
        for workspace_id in await list_tenant_workspace_ids(tenant_id):
            applied.extend(await apply_workflow_capabilities(tenant_id, workspace_id))
    await _audit_admin(
        tenant_id, "tenant:workflow_set", "tenant", {"workflow_key": body.workflow_key or ""}
    )
    return {"workflow_key": body.workflow_key or "", "capabilities_applied": sorted(set(applied))}


# ── vocabulary overlay (the tenant's pinned display vocabulary) ───────────────────────
class OverlayOut(BaseModel):
    key: str
    name: str


class SetOverlayRequest(BaseModel):
    overlay_key: str | None = None


@router.get("/tenants/{tenant_id}/overlays")
async def list_overlays_endpoint(tenant_id: uuid.UUID) -> list[OverlayOut]:
    """Overlays available to this tenant (system overlays + its own). The tenant's current
    pick is on the tenant row (TenantOut.overlay_key)."""
    return [OverlayOut(key=o.key, name=o.name) for o in await list_overlays(tenant_id)]


@router.post("/tenants/{tenant_id}/overlay")
async def set_overlay_endpoint(tenant_id: uuid.UUID, body: SetOverlayRequest) -> dict[str, str]:
    """Pin the tenant's display vocabulary. Regular users can't change it (the per-workspace
    switcher is removed from the app); every workspace resolves to this by default."""
    await set_tenant_default_overlay(tenant_id, body.overlay_key)
    await _audit_admin(
        tenant_id, "tenant:overlay_set", "tenant", {"overlay_key": body.overlay_key or ""}
    )
    return {"overlay_key": body.overlay_key or ""}


# ── plugin repositories (workflow providers; see core/plugins) ───────────────────────
class DeclaredServerOut(BaseModel):
    workflow_key: str
    key: str
    url: str
    enabled_tools: list[str]
    # A `pyrrhula://` url is the platform's own in-process tooling (the randomizer, git delegation),
    # reached over no network and exposed to nobody. Only a real transport -- http(s), ws,
    # stdio -- is a third party the operator is actually approving. Conflating the two made
    # a clean install look like it had attached external MCP servers when it had not.
    external: bool = False


class PluginRepoOut(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    ref: str
    source: str
    status: str
    last_error: str
    workflow_keys: list[str]
    last_synced_at: datetime | None
    # The MCP servers this repository's workflows would enable on workspaces -- the
    # operator's review surface: adding a repo is also approving these endpoints.
    # Built-in `pyrrhula://` entries are included but flagged `external: false`; only the
    # external ones are an approval decision.
    declared_servers: list[DeclaredServerOut] = []


class AddPluginRepoRequest(BaseModel):
    name: str
    url: str
    ref: str


async def _plugin_out(row: object) -> PluginRepoOut:
    out = PluginRepoOut.model_validate(row, from_attributes=True)
    templates = {w.key: w for w in await list_workflows()}
    for key in out.workflow_keys:
        workflow = templates.get(key)
        if workflow is None:
            continue
        declared = (workflow.capabilities or {}).get("mcp_servers")
        for spec in declared if isinstance(declared, list) else []:
            url = str(spec.get("url", ""))
            out.declared_servers.append(
                DeclaredServerOut(
                    workflow_key=key,
                    key=str(spec.get("key", "")),
                    url=url,
                    enabled_tools=[str(t) for t in spec.get("enabled_tools", [])],
                    external=is_external_mcp_url(url),
                )
            )
    return out


class RetrievalModelsBody(BaseModel):
    embedding_model: str
    embedding_dimension: int
    reranker_model: str = ""
    reranker_enabled: bool = True


class RetrievalModelsResponse(RetrievalModelsBody):
    source: str = "environment"
    # What a change to the embedding model would orphan. Shown, not enforced: an operator
    # is allowed to make this decision, but not to make it without seeing the cost.
    embedded_chunks: int = 0
    applies: str = "on the next restart of the api and worker"


@router.get("/retrieval-models")
async def get_retrieval_models_endpoint() -> RetrievalModelsResponse:
    """Which models this deployment embeds and reranks with."""
    from core.deployment_settings import embedded_chunk_count, get_retrieval_models

    effective = await get_retrieval_models()
    return RetrievalModelsResponse(**effective, embedded_chunks=await embedded_chunk_count())


class AdminAssistantModelBody(BaseModel):
    provider: str = ""
    model: str = ""
    api_base: str | None = None
    # Write-only: never returned, and an empty value keeps the stored key.
    api_key: str | None = None


@router.get("/assistant/model")
async def get_admin_assistant_model_endpoint() -> AdminAssistantModelBody:
    """Which connection the admin assistant runs on. The key is never returned."""
    from core.admin.assistant import get_admin_connection

    row = await get_admin_connection()
    if row is None:
        return AdminAssistantModelBody()
    return AdminAssistantModelBody(provider=row.provider, model=row.model, api_base=row.api_base)


@router.put("/assistant/model")
async def set_admin_assistant_model_endpoint(
    body: AdminAssistantModelBody,
) -> AdminAssistantModelBody:
    """Point the admin assistant at a model.

    A connection on the reserved admin tenant rather than a new deployment setting, so it
    reuses the encrypted-credential storage and egress policy the rest of the product has
    instead of inventing a thinner second way to hold a provider key."""
    from api.encryptor_factory import get_encryptor
    from core.admin.assistant import set_admin_connection

    if not body.provider.strip() or not body.model.strip():
        raise HTTPException(status_code=422, detail="provider and model are both required")
    row = await set_admin_connection(
        provider=body.provider.strip(),
        model=body.model.strip(),
        api_base=(body.api_base or "").strip() or None,
        api_key=(body.api_key or "").strip() or None,
        encryptor=get_encryptor(),
    )
    return AdminAssistantModelBody(provider=row.provider, model=row.model, api_base=row.api_base)


class AdminAssistantChatRequest(BaseModel):
    messages: list[dict[str, str]] = []


@router.post("/assistant/chat")
async def admin_assistant_chat_endpoint(body: AdminAssistantChatRequest) -> StreamingResponse:
    """The admin console's assistant: the same NDJSON event stream the workspace widget
    speaks, over deployment state rather than workspace knowledge.

    It proposes and never applies: a proposal streams back as a card, and the operator's
    Apply click calls the ordinary admin endpoint from their own session. The assistant
    holds no privilege of its own -- which, on the one screen where a mistake is
    deployment-wide, is the reason it can exist at all."""
    from api.encryptor_factory import get_encryptor
    from api.model_provider_factory import get_model_provider
    from core.admin.assistant import admin_chat, get_admin_connection
    from core.agents.authoring import resolve_connection_api_key
    from core.tenancy.admin import ADMIN_TENANT_ID

    connection = await get_admin_connection()
    if connection is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "no admin assistant model configured -- set one under Assistant on this page first"
            ),
        )
    api_key = (
        await resolve_connection_api_key(
            ADMIN_TENANT_ID, str(connection.credential_ref), encryptor=get_encryptor()
        )
        if connection.credential_ref
        else None
    )

    async def _ndjson() -> AsyncIterator[str]:
        async for event in admin_chat(
            body.messages,
            provider_factory=get_model_provider,
            connection=connection,
            api_key=api_key,
        ):
            yield json.dumps(event) + "\n"

    return StreamingResponse(_ndjson(), media_type="application/x-ndjson")


@router.get("/retrieval-models/cache")
async def retrieval_cache_status_endpoint() -> dict[str, object]:
    """Whether the models are actually on this box, and how big they are.

    The installers used to block on the download, so "did it work" was answered by the
    install finishing. Now that it does not, something has to be able to say."""
    from core.retrieval_cache import cache_status

    return await cache_status()


@router.post("/retrieval-models/download", status_code=202)
async def download_retrieval_models_endpoint() -> dict[str, object]:
    """Fetch the configured models from Hugging Face, in the background.

    202 and a job rather than a long request: this is gigabytes, and an operator who
    closes the tab should not cancel it. Safe to press twice -- an already-cached model
    costs a metadata check."""
    from api.job_queue_factory import get_job_queue
    from core.tenancy.admin import ADMIN_TENANT_ID

    await get_job_queue().enqueue(ADMIN_TENANT_ID, "download_retrieval_models", {})
    return {"status": "queued"}


@router.post("/retrieval-models/upload", status_code=201)
async def upload_retrieval_cache_endpoint(file: UploadFile = File(...)) -> dict[str, object]:
    """Install an operator-supplied Hugging Face cache tarball.

    The air-gapped path: a box with no route to huggingface.co cannot download, and
    waiting for one is not a deployment story. Fetch the cache where there is a route,
    `tar czf` the hub directory, upload it here.
    """
    from core.retrieval_cache import CacheUploadError, install_from_tarball

    try:
        return await run_in_threadpool(
            install_from_tarball, file.file, max_bytes=_MAX_CACHE_UPLOAD_BYTES
        )
    except CacheUploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/retrieval-models")
async def set_retrieval_models_endpoint(body: RetrievalModelsBody) -> RetrievalModelsResponse:
    """Override the configured models.

    Deployment-level rather than per tenant: every tenant's vectors live in one column of
    one width, so "which embedding model" cannot coherently differ between them.

    Takes effect on the next restart -- the providers hold a loaded model, and swapping it
    under a running process would change what a half-finished retrieval means partway
    through. Changing the embedding model also orphans every existing vector, because
    embeddings from different models are not comparable; that content needs re-indexing.
    """
    from core.deployment_settings import (
        embedded_chunk_count,
        get_retrieval_models,
        set_retrieval_models,
    )

    try:
        await set_retrieval_models(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    effective = await get_retrieval_models()
    return RetrievalModelsResponse(**effective, embedded_chunks=await embedded_chunk_count())


@router.get("/plugin-repositories")
async def list_plugin_repos_endpoint() -> list[PluginRepoOut]:
    from core.plugins.service import list_repositories

    return [await _plugin_out(r) for r in await list_repositories()]


@router.post("/plugin-repositories", status_code=201)
async def add_plugin_repo_endpoint(body: AddPluginRepoRequest) -> PluginRepoOut:
    from core.plugins.service import PluginSyncError, add_repository

    try:
        return await _plugin_out(await add_repository(body.name, body.url, body.ref))
    except PluginSyncError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/plugin-repositories/upload", status_code=201)
async def upload_plugin_endpoint(
    name: str = Form(...), file: UploadFile = File(...)
) -> PluginRepoOut:
    """Install a workflow pack from an uploaded .zip/.tar.gz.

    The no-git path: a deployment that cannot reach the pinned plugin repositories (air
    gapped, or the repo is private) gets its packs this way, with no credentials and no
    restart. The drop directory (PYRRHULA_PLUGIN_DROP_DIR) is the same capability for
    operators who would rather mount a folder.
    """
    from core.plugins.service import PluginSyncError, install_uploaded_plugin

    try:
        return await _plugin_out(await install_uploaded_plugin(name.strip(), await file.read()))
    except PluginSyncError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/plugin-repositories/{repository_id}/sync")
async def sync_plugin_repo_endpoint(repository_id: uuid.UUID) -> PluginRepoOut:
    from core.plugins.service import PluginSyncError, sync_repository

    try:
        return await _plugin_out(await sync_repository(repository_id))
    except PluginSyncError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/plugin-repositories/{repository_id}", status_code=204)
async def remove_plugin_repo_endpoint(repository_id: uuid.UUID) -> None:
    from core.plugins.service import PluginSyncError, remove_repository

    try:
        await remove_repository(repository_id)
    except PluginSyncError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tenants", status_code=201)
async def create_tenant_endpoint(body: CreateTenantRequest) -> dict[str, str]:
    try:
        tenant_id, workspace_id = await create_tenant(body.name, body.slug)
    except TenantExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    owner_principal_id: uuid.UUID | None = None
    if body.owner_email is not None:
        if not body.owner_password or len(body.owner_password) < 8:
            raise HTTPException(
                status_code=400, detail="owner_password must be at least 8 characters"
            )
        owner_principal_id = await _create_user(
            tenant_id, body.owner_email, body.owner_password, body.owner_display_name, "owner"
        )
        # The same grant self-serve signup makes, for the same reason: conducting,
        # entity work and MCP registration are workspace-gated, and a tenant role alone
        # satisfies none of them. Without this an admin-created owner landed in a
        # default workspace nobody was a member of and hit "not a member of this
        # workspace" on the first thing they tried.
        from core.tenancy.models import WorkspaceMembership
        from core.tenancy.scope import tenant_scope as _tenant_scope

        async with _tenant_scope(tenant_id) as session:
            session.add(
                WorkspaceMembership(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    principal_id=owner_principal_id,
                    role="steward",
                )
            )
    return {
        "tenant_id": str(tenant_id),
        "workspace_id": str(workspace_id),
        "owner_principal_id": str(owner_principal_id) if owner_principal_id else "",
    }


@router.post("/tenants/{tenant_id}/deactivate")
async def deactivate_tenant_endpoint(tenant_id: uuid.UUID) -> dict[str, bool]:
    if tenant_id == ADMIN_TENANT_ID:
        raise HTTPException(status_code=409, detail="the admin tenant cannot be deactivated")
    await set_tenant_deactivated(tenant_id, True)
    return {"deactivated": True}


@router.post("/tenants/{tenant_id}/reactivate")
async def reactivate_tenant_endpoint(tenant_id: uuid.UUID) -> dict[str, bool]:
    await set_tenant_deactivated(tenant_id, False)
    return {"deactivated": False}


# ── tenant MCP capabilities (admin-attached, no custom workflow needed) ──────────────
class McpCapabilityOut(BaseModel):
    key: str
    url: str
    enabled_tools: list[str]
    effectful_tools: list[str]
    credential_ref: str | None
    require_confirmation: bool


class PutMcpCapabilityRequest(BaseModel):
    key: str
    url: str
    enabled_tools: list[str]
    effectful_tools: list[str] = []
    credential_ref: str | None = None
    require_confirmation: bool = True


@router.get("/tenants/{tenant_id}/mcp-servers")
async def list_tenant_mcp_endpoint(tenant_id: uuid.UUID) -> list[McpCapabilityOut]:
    from core.mcp.registry import list_tenant_capabilities

    return [
        McpCapabilityOut(
            key=g.key,
            url=g.url,
            enabled_tools=list(g.enabled_tools),
            effectful_tools=list(g.effectful_tools),
            credential_ref=g.credential_ref,
            require_confirmation=g.require_confirmation,
        )
        for g in await list_tenant_capabilities(tenant_id)
    ]


@router.put("/tenants/{tenant_id}/mcp-servers")
async def put_tenant_mcp_endpoint(
    tenant_id: uuid.UUID, body: PutMcpCapabilityRequest
) -> dict[str, object]:
    """Attach (or update) an MCP capability grant on a tenant and materialize it onto
    every workspace immediately. The grant survives workflow re-applies and reaches
    workspaces created later."""
    from core.mcp.registry import (
        CredentialInRegistryError,
        apply_tenant_capabilities,
        upsert_tenant_capability,
    )

    try:
        await upsert_tenant_capability(
            tenant_id,
            body.key,
            body.url,
            enabled_tools=body.enabled_tools,
            effectful_tools=body.effectful_tools,
            credential_ref=body.credential_ref,
            require_confirmation=body.require_confirmation,
        )
    except CredentialInRegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    workspaces = await list_tenant_workspace_ids(tenant_id)
    for workspace_id in workspaces:
        await apply_tenant_capabilities(tenant_id, workspace_id)
    await _audit_admin(
        tenant_id,
        "tenant:mcp_grant",
        "tenant",
        {"key": body.key, "url": body.url, "enabled_tools": body.enabled_tools},
    )
    return {"key": body.key, "workspaces_applied": len(workspaces)}


@router.delete("/tenants/{tenant_id}/mcp-servers/{key}", status_code=204)
async def delete_tenant_mcp_endpoint(tenant_id: uuid.UUID, key: str) -> None:
    from core.mcp.registry import delete_tenant_capability

    if not await delete_tenant_capability(tenant_id, key):
        raise HTTPException(status_code=404, detail=f"no MCP capability {key!r} on this tenant")
    await _audit_admin(tenant_id, "tenant:mcp_revoke", "tenant", {"key": key})


# ── egress policy (D14: which provider kinds each purpose may reach) ─────────────────
_EGRESS_PURPOSES = {"generation", "gate", "rerank", "embed", "report", "rewrite", "delegation"}
_EGRESS_KINDS = {"local", "cloud"}


class EgressPolicyBody(BaseModel):
    policy: dict[str, list[str]]


@router.get("/tenants/{tenant_id}/egress-policy")
async def get_egress_policy_endpoint(tenant_id: uuid.UUID) -> dict[str, object]:
    from core.tenancy.egress import load_egress_policy

    return {"policy": await load_egress_policy(tenant_id)}


@router.put("/tenants/{tenant_id}/egress-policy")
async def put_egress_policy_endpoint(
    tenant_id: uuid.UUID, body: EgressPolicyBody
) -> dict[str, object]:
    """Set the tenant's D14 egress policy: {purpose: ["local"] | ["local","cloud"]}.
    An absent purpose stays permissive (the plan's explicit default); an empty list
    blocks that purpose entirely."""
    for purpose, kinds in body.policy.items():
        if purpose not in _EGRESS_PURPOSES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown purpose {purpose!r}; valid: {sorted(_EGRESS_PURPOSES)}",
            )
        if not set(kinds) <= _EGRESS_KINDS:
            raise HTTPException(
                status_code=422,
                detail=f"purpose {purpose!r}: kinds must be within {sorted(_EGRESS_KINDS)}",
            )
    from core.tenancy.egress import invalidate_egress_policy
    from core.tenancy.models import Tenant
    from core.tenancy.scope import unscoped_session

    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail=f"no tenant {tenant_id}")
        settings = dict(tenant.settings)
        settings["egress_policy"] = body.policy
        tenant.settings = settings
    invalidate_egress_policy(tenant_id)
    await _audit_admin(tenant_id, "tenant:egress_policy_set", "tenant", dict(body.policy))
    return {"policy": body.policy}


# ── users ────────────────────────────────────────────────────────────────────────────
class UserOut(BaseModel):
    principal_id: uuid.UUID
    display_name: str
    role: str
    email: str | None
    disabled: bool


class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str
    display_name: str
    role: str = "participant"


@router.get("/tenants/{tenant_id}/users")
async def list_users_endpoint(tenant_id: uuid.UUID) -> list[UserOut]:
    return [
        UserOut(
            principal_id=u.principal_id,
            display_name=u.display_name,
            role=u.role,
            email=u.email,
            disabled=u.disabled_at is not None,
        )
        for u in await list_tenant_users(tenant_id)
    ]


@router.post("/tenants/{tenant_id}/users", status_code=201)
async def create_user_endpoint(tenant_id: uuid.UUID, body: CreateUserRequest) -> dict[str, str]:
    if body.role not in _ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {sorted(_ROLES)}")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="password must be at least 8 characters")
    principal_id = await _create_user(
        tenant_id, body.email, body.password, body.display_name, body.role
    )
    return {"principal_id": str(principal_id)}


@router.post("/tenants/{tenant_id}/users/{principal_id}/deactivate")
async def deactivate_user_endpoint(
    tenant_id: uuid.UUID, principal_id: uuid.UUID
) -> dict[str, bool]:
    try:
        await set_principal_disabled(tenant_id, principal_id, True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"disabled": True}


@router.post("/tenants/{tenant_id}/users/{principal_id}/reactivate")
async def reactivate_user_endpoint(
    tenant_id: uuid.UUID, principal_id: uuid.UUID
) -> dict[str, bool]:
    try:
        await set_principal_disabled(tenant_id, principal_id, False)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"disabled": False}


async def _create_user(
    tenant_id: uuid.UUID, email: str, password: str, display_name: str, role: str
) -> uuid.UUID:
    """principal + membership, then the login identity -- with the same orphan-cleanup on a
    duplicate-email race that ``api.routes.auth.register`` does."""
    principal_id = await create_tenant_user(tenant_id, display_name, role)
    try:
        await _identity_provider.register_local(tenant_id, principal_id, email, password)
    except Exception as exc:  # noqa: BLE001 -- unique-constraint race -> clean up + 409
        await delete_principal(tenant_id, principal_id)
        raise HTTPException(status_code=409, detail="email already registered") from exc
    return principal_id


# ── who may join an organization ────────────────────────────────────────────────────
class RegistrationPolicyRequest(BaseModel):
    policy: str


class RejectRequest(BaseModel):
    note: str | None = None


@router.get("/tenants/{tenant_id}/registration-policy")
async def get_registration_policy_endpoint(tenant_id: uuid.UUID) -> dict[str, object]:
    """This tenant's policy, the deployment default, and what the choices mean."""
    from core.config import get_settings as _s
    from core.tenancy.registration import POLICIES, get_policy, normalise_policy

    return {
        "policy": await get_policy(tenant_id),
        "deployment_default": normalise_policy(_s().default_registration_policy),
        "choices": sorted(POLICIES),
    }


@router.put("/tenants/{tenant_id}/registration-policy")
async def set_registration_policy_endpoint(
    tenant_id: uuid.UUID, body: RegistrationPolicyRequest
) -> dict[str, str]:
    from core.tenancy.registration import set_policy

    try:
        chosen = await set_policy(tenant_id, body.policy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _audit_admin(tenant_id, "tenant.registration_policy", "tenant", {"policy": chosen})
    return {"policy": chosen}


@router.get("/tenants/{tenant_id}/registration-requests")
async def list_registration_requests_endpoint(tenant_id: uuid.UUID) -> list[dict[str, str]]:
    from core.tenancy.registration import list_pending

    return [
        {
            "id": str(r.id),
            "email": r.email,
            "display_name": r.display_name,
            "created_at": r.created_at.isoformat(),
        }
        for r in await list_pending(tenant_id)
    ]


@router.post("/tenants/{tenant_id}/registration-requests/{request_id}/approve")
async def approve_registration_endpoint(
    tenant_id: uuid.UUID, request_id: uuid.UUID, role: str = "viewer"
) -> dict[str, str]:
    """Turn an application into an account, at the role the admin chooses."""
    from core.tenancy.registration import take_pending

    if role not in _ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {sorted(_ROLES)}")
    # Claimed and marked decided in one statement, so two admins approving at once
    # cannot both go on to mint an account.
    claimed = await take_pending(tenant_id, request_id, None)
    if claimed is None:
        raise HTTPException(status_code=404, detail="no pending request with that id")

    principal_id = await create_tenant_user(tenant_id, claimed.display_name, role)
    try:
        await _identity_provider.register_local_hashed(
            tenant_id, principal_id, claimed.email, claimed.password_hash
        )
    except Exception as exc:  # noqa: BLE001 -- duplicate email -> clean up, report
        await delete_principal(tenant_id, principal_id)
        raise HTTPException(status_code=409, detail="email already registered") from exc
    await _audit_admin(
        tenant_id,
        "tenant.registration_approved",
        "principal",
        {"email": claimed.email, "role": role},
    )
    return {"principal_id": str(principal_id), "email": claimed.email, "role": role}


@router.post("/tenants/{tenant_id}/registration-requests/{request_id}/reject")
async def reject_registration_endpoint(
    tenant_id: uuid.UUID, request_id: uuid.UUID, body: RejectRequest | None = None
) -> dict[str, bool]:
    from core.tenancy.registration import reject

    if not await reject(tenant_id, request_id, None, (body.note if body else None)):
        raise HTTPException(status_code=404, detail="no pending request with that id")
    await _audit_admin(tenant_id, "tenant.registration_rejected", "tenant", {})
    return {"rejected": True}


# ── audit chain verification (E2.10's verifier, as an operable endpoint) ─────────────
@router.get("/tenants/{tenant_id}/audit/verify")
async def verify_audit_chain_endpoint(tenant_id: uuid.UUID) -> dict[str, object]:
    """Recompute every audit row's hash chain for this tenant and report breaks.
    Tamper-EVIDENT storage is only worth what its tamper-DETECTION run is worth; this
    is the operable entry point (cron it, or watch it from the admin console)."""
    from core.audit.verify import verify_chain

    broken = await verify_chain(tenant_id)
    return {
        "tenant_id": str(tenant_id),
        "ok": not broken,
        "broken_row_ids": [str(r) for r in broken],
    }

"""Minimal read-only workspace/agent listing (T0.9): the frontend's workspace-list and
agent-select flow needs something real to list against. Full workspace/agent management
(create, edit, delete) is Phase 1 scope (D1.x) — this adds only the read side needed to
make the login -> workspace list -> session flow genuinely functional rather than
requiring hardcoded ids pasted into the UI.

G4.2 adds the two between-session surfaces: advancing the workspace clock (an explicit,
permission-gated act that enqueues the schedule-effect job rather than applying it inline)
and the change feed (records, not prose, filtered by the caller's own scope set).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db_session
from api.job_queue_factory import get_job_queue
from api.middleware.auth import get_request_context
from api.middleware.rate_limit import rate_limit_by_principal, rate_limit_by_tenant
from api.permission_service_factory import get_permission_service
from core.agents.models import Persona
from core.assembler.visibility import EXPORT, scopes_for
from core.entities.schedule import (
    ClockPermissionDeniedError,
    ClockRewindError,
    advance_clock,
    get_clock,
    list_change_feed,
)
from core.tenancy.context import RequestContext
from core.tenancy.models import Workspace

# Assignable workspace roles, including the combined `steward` seat a creator gets by
# default -- an operator adding a second solo-style member should be able to grant it too.
from core.tenancy.roles import WORKSPACE_ROLES as _WORKSPACE_ROLES

router = APIRouter(
    prefix="/workspaces",
    tags=["workspaces"],
    dependencies=[
        Depends(get_request_context),
        Depends(rate_limit_by_principal),
        Depends(rate_limit_by_tenant),
    ],
)


class WorkspaceResponse(BaseModel):
    id: uuid.UUID
    key: str
    name: str


@router.get("")
async def list_workspaces(
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[WorkspaceResponse]:
    rows = (
        (
            await session.execute(
                select(Workspace).where(
                    Workspace.tenant_id == ctx.tenant_id,
                    Workspace.archived_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return [WorkspaceResponse(id=w.id, key=w.key, name=w.name) for w in rows]


class PersonaResponse(BaseModel):
    id: uuid.UUID
    key: str
    name: str


class CreateWorkspaceRequest(BaseModel):
    name: str
    key: str | None = None


@router.post("", status_code=201)
async def create_workspace_endpoint(
    body: CreateWorkspaceRequest, ctx: RequestContext = Depends(get_request_context)
) -> WorkspaceResponse:
    """Self-serve workspace creation (tenant owners): default scopes seeded, creator
    granted the overseer workspace role (conducting/inspection are workspace-gated)."""
    import re as _re

    from api.authz import require_tenant_permission
    from core.tenancy.models import WorkspaceMembership
    from core.tenancy.provisioning import TenantExistsError, create_workspace
    from core.tenancy.scope import tenant_scope

    await require_tenant_permission(ctx, "manage_tenant")
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    key = (body.key or _re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-"))[:60]
    if not key:
        raise HTTPException(status_code=400, detail="could not derive a workspace key")
    try:
        workspace_id = await create_workspace(ctx.tenant_id, key, body.name.strip())
    except TenantExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    async with tenant_scope(ctx.tenant_id) as session:
        session.add(
            WorkspaceMembership(
                tenant_id=ctx.tenant_id,
                workspace_id=workspace_id,
                principal_id=ctx.principal_id,
                role="steward",
            )
        )
    # A workspace born after the tenant's workflow was pinned (or after admin MCP
    # grants were attached) still gets the full capability set.
    from core.workflows.service import apply_workflow_capabilities

    await apply_workflow_capabilities(ctx.tenant_id, workspace_id)
    return WorkspaceResponse(id=workspace_id, key=key, name=body.name.strip())


class WorkspaceSettingsBody(BaseModel):
    # How held secrets reach their holder's context in secret-capable phases:
    # "excluded" (default) never; "trust" always, relying on the acting model to play
    # them; "gate" through a per-turn classifier whose verdict exclusion enforces.
    secret_mode: str | None = None
    # Legacy boolean, kept for old clients: true meant what "gate" means now.
    secrets_gate: bool | None = None
    # Free-text conduct/style rules appended to every agent turn's system prompt in
    # this workspace (e.g. "reply in short paragraphs; never write another
    # character's dialogue"). Content, not code -- the owner edits it in the UI.
    conduct_rules: str | None = None
    # Whether an agent may merge an approved PR itself (True) or merging is a human's job
    # (False, the default). Off is the conservative posture: a merge is consequential.
    allow_automerge: bool | None = None
    # Values resolved through the settings chain (core/settings/resolve.py). Omitting a
    # field leaves it as it is; sending null CLEARS it, which is how a workspace goes back
    # to inheriting its tenant's value rather than storing an empty one.
    max_review_rounds: int | None = None
    moderation_model: str | None = None


@router.get("/{workspace_id}/settings")
async def get_workspace_settings(
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(ctx.tenant_id) as session:
        row = await session.get(Workspace, workspace_id)
        if row is None or row.tenant_id != ctx.tenant_id:
            raise HTTPException(status_code=404, detail="no such workspace")
        return {"settings": dict(row.settings)}


@router.patch("/{workspace_id}/settings")
async def patch_workspace_settings(
    workspace_id: uuid.UUID,
    body: WorkspaceSettingsBody,
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    """Owner-facing workspace switches. `secrets_gate` opts the workspace into the
    disclosure gate (one extra small model call per secret-holding turn in phases that
    grant secret visibility) -- off, held secrets simply never enter context at all:
    still leak-proof, but never a hint or an in-character reveal either."""
    allowed = await get_permission_service().check(
        ctx.tenant_id, ctx.principal_id, "manage_workspace", "workspace", workspace_id
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="manage_workspace required")
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(ctx.tenant_id) as session:
        row = await session.get(Workspace, workspace_id)
        if row is None or row.tenant_id != ctx.tenant_id:
            raise HTTPException(status_code=404, detail="no such workspace")
        settings = dict(row.settings)
        if body.secret_mode is not None:
            if body.secret_mode not in ("excluded", "trust", "gate"):
                raise HTTPException(
                    status_code=422,
                    detail="secret_mode must be one of: excluded, trust, gate",
                )
            settings["secret_mode"] = body.secret_mode
            # Keep the legacy boolean coherent for anything still reading it.
            settings["secrets_gate"] = body.secret_mode == "gate"
        if body.secrets_gate is not None:
            settings["secrets_gate"] = body.secrets_gate
            settings.setdefault("secret_mode", "gate" if body.secrets_gate else "excluded")
            if body.secret_mode is None:
                settings["secret_mode"] = "gate" if body.secrets_gate else "excluded"

        if body.conduct_rules is not None:
            settings["conduct_rules"] = body.conduct_rules.strip()
        if body.allow_automerge is not None:
            settings["allow_automerge"] = body.allow_automerge
        # Inheritable keys: present-and-null means "stop overriding", which is removing
        # the key rather than storing a falsy value the resolver would treat as a choice.
        for field_name in ("max_review_rounds", "moderation_model"):
            if field_name not in body.model_fields_set:
                continue
            value = getattr(body, field_name)
            if value is None:
                settings.pop(field_name, None)
            else:
                settings[field_name] = value
        row.settings = settings
    return {"settings": settings}


@router.get("/{workspace_id}/agents")
async def list_personas(
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
    session: AsyncSession = Depends(get_db_session),
) -> list[PersonaResponse]:
    rows = (
        (
            await session.execute(
                select(Persona).where(
                    Persona.tenant_id == ctx.tenant_id, Persona.workspace_id == workspace_id
                )
            )
        )
        .scalars()
        .all()
    )
    return [PersonaResponse(id=a.id, key=a.key, name=a.name) for a in rows]


# ── G4.2: between-session state ─────────────────────────────────────────────────────


class ClockResponse(BaseModel):
    clock_value: int


class AdvanceClockRequest(BaseModel):
    to_value: int


class AdvanceClockResponse(BaseModel):
    from_clock: int
    to_clock: int
    job_id: uuid.UUID


@router.get("/{workspace_id}/clock")
async def get_workspace_clock(
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
) -> ClockResponse:
    """A read, and only a read. The acceptance criterion "the clock never advances as a
    side effect of reads" is met by this endpoint having no write path to reach for."""
    return ClockResponse(clock_value=await get_clock(ctx.tenant_id, workspace_id))


@router.post("/{workspace_id}/clock", status_code=202)
async def advance_workspace_clock(
    workspace_id: uuid.UUID,
    body: AdvanceClockRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> AdvanceClockResponse:
    """202, not 200: the clock moves synchronously (it is one UPDATE, and the caller needs
    to see it), but the schedule effects the move made due are a worker job. A long-dormant
    workspace advancing a year can fan out into a great many mutations, and none of them
    belong inside an HTTP request."""
    try:
        advance = await advance_clock(
            ctx.tenant_id,
            workspace_id,
            ctx.principal_id,
            body.to_value,
            permission_service=get_permission_service(),
        )
    except ClockPermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ClockRewindError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    job_id = await get_job_queue().enqueue(
        ctx.tenant_id,
        "apply_due_schedules",
        {
            "tenant_id": str(ctx.tenant_id),
            "workspace_id": str(workspace_id),
            "principal_id": str(ctx.principal_id),
            "from_clock": advance.from_clock,
            "to_clock": advance.to_clock,
        },
    )
    return AdvanceClockResponse(
        from_clock=advance.from_clock, to_clock=advance.to_clock, job_id=job_id
    )


class ChangeFeedItem(BaseModel):
    change_id: uuid.UUID
    entity_id: uuid.UUID
    field_path: str
    old_value: object | None
    new_value: object | None
    cause: str
    cause_ref: str | None
    in_session: bool
    created_at: datetime


@router.get("/{workspace_id}/changes")
async def list_workspace_changes(
    workspace_id: uuid.UUID,
    since: datetime | None = None,
    out_of_session_only: bool = False,
    ctx: RequestContext = Depends(get_request_context),
) -> list[ChangeFeedItem]:
    """The between-sessions change feed: rows, never prose. Scoped through the same
    resolver everything else uses, at the ``EXPORT`` pseudo-phase — a workspace-level feed
    has no phase to narrow by, and ``EXPORT`` is precisely "everything in this workspace
    this principal is entitled to" (C1.1)."""
    scope_set = await scopes_for(ctx.tenant_id, ctx.principal_id, workspace_id, EXPORT, None)
    rows = await list_change_feed(
        ctx.tenant_id,
        workspace_id,
        frozenset(scope_set),
        since=since,
        out_of_session_only=out_of_session_only,
    )
    return [
        ChangeFeedItem(
            change_id=row.change_id,
            entity_id=row.entity_id,
            field_path=row.field_path,
            old_value=row.old_value,
            new_value=row.new_value,
            cause=row.cause,
            cause_ref=row.cause_ref,
            in_session=row.in_session,
            created_at=row.created_at,
        )
        for row in rows
    ]


# ── workspace members (the multi-user seam the audit found missing entirely) ─────────


class WorkspaceMemberOut(BaseModel):
    principal_id: uuid.UUID
    display_name: str
    role: str
    email: str | None
    is_persona: bool


class AddWorkspaceMemberRequest(BaseModel):
    email: str
    role: str = "participant"


@router.get("/{workspace_id}/members")
async def list_workspace_members(
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
) -> list[WorkspaceMemberOut]:
    from core.tenancy.models import Identity, Principal, WorkspaceMembership
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(ctx.tenant_id) as session:
        rows = (
            await session.execute(
                select(
                    WorkspaceMembership.principal_id,
                    WorkspaceMembership.role,
                    Principal.display_name,
                    Principal.kind,
                    Identity.external_id,
                )
                .join(Principal, Principal.id == WorkspaceMembership.principal_id)
                .join(
                    Identity,
                    (Identity.principal_id == Principal.id) & (Identity.provider == "local"),
                    isouter=True,
                )
                .where(WorkspaceMembership.workspace_id == workspace_id)
            )
        ).all()
    return [
        WorkspaceMemberOut(
            principal_id=pid,
            display_name=name,
            role=role,
            email=email,
            is_persona=kind != "human",
        )
        for pid, role, name, kind, email in rows
    ]


@router.post("/{workspace_id}/members", status_code=201)
async def add_workspace_member(
    workspace_id: uuid.UUID,
    body: AddWorkspaceMemberRequest,
    ctx: RequestContext = Depends(get_request_context),
) -> WorkspaceMemberOut:
    """Add a HUMAN teammate to this workspace by their login email. Personas join via
    persona creation; this endpoint is people only. Roles: facilitator can conduct,
    overseer can inspect, participant acts, viewer reads."""
    from core.tenancy.models import Identity, Principal, WorkspaceMembership
    from core.tenancy.scope import tenant_scope

    allowed = await get_permission_service().check(
        ctx.tenant_id, ctx.principal_id, "manage_workspace", "workspace", workspace_id
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="manage_workspace required")
    if body.role not in _WORKSPACE_ROLES:
        raise HTTPException(
            status_code=400, detail=f"role must be one of {sorted(_WORKSPACE_ROLES)}"
        )
    async with tenant_scope(ctx.tenant_id) as session:
        row = (
            await session.execute(
                select(Identity.principal_id, Principal.display_name)
                .join(Principal, Principal.id == Identity.principal_id)
                .where(Identity.provider == "local", Identity.external_id == body.email)
            )
        ).first()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="no user with that email in this organization -- create them first",
            )
        principal_id, display_name = row
        existing = await session.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.principal_id == principal_id,
            )
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="already a member of this workspace")
        session.add(
            WorkspaceMembership(
                tenant_id=ctx.tenant_id,
                workspace_id=workspace_id,
                principal_id=principal_id,
                role=body.role,
            )
        )
    from core.audit.service import AuditService

    await AuditService().append(
        tenant_id=ctx.tenant_id,
        actor_principal_id=ctx.principal_id,
        action="workspace:member_add",
        resource_type="workspace",
        resource_id=workspace_id,
        target_ids=[principal_id],
    )
    return WorkspaceMemberOut(
        principal_id=principal_id,
        display_name=display_name,
        role=body.role,
        email=body.email,
        is_persona=False,
    )


@router.delete("/{workspace_id}/members/{principal_id}", status_code=204)
async def remove_workspace_member(
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
) -> None:
    from sqlalchemy import delete as sa_delete

    from core.tenancy.models import Principal, WorkspaceMembership
    from core.tenancy.scope import tenant_scope

    allowed = await get_permission_service().check(
        ctx.tenant_id, ctx.principal_id, "manage_workspace", "workspace", workspace_id
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="manage_workspace required")
    async with tenant_scope(ctx.tenant_id) as session:
        kind = await session.scalar(select(Principal.kind).where(Principal.id == principal_id))
        if kind is not None and kind != "human":
            raise HTTPException(
                status_code=409,
                detail="personas leave a workspace by being archived, not removed here",
            )
        await session.execute(
            sa_delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.principal_id == principal_id,
            )
        )
    from core.audit.service import AuditService

    await AuditService().append(
        tenant_id=ctx.tenant_id,
        actor_principal_id=ctx.principal_id,
        action="workspace:member_remove",
        resource_type="workspace",
        resource_id=workspace_id,
        target_ids=[principal_id],
    )


@router.delete("/{workspace_id}", status_code=204)
async def archive_workspace_endpoint(
    workspace_id: uuid.UUID,
    ctx: RequestContext = Depends(get_request_context),
) -> None:
    """Archive a workspace: it leaves the list, and everything in it stops being offered.

    Soft, like every other delete in this product (sessions, personas, knowledge). The
    hard reason is that a workspace owns append-only history -- audit rows, disclosure
    events, resolution records -- which the app role has no grant to delete and which a
    tenant may be required to retain. The superuser purge CLI remains the only path that
    truly reclaims rows.

    Refuses the tenant's last unarchived workspace: a tenant with nowhere to work is a
    dead end a user cannot get out of through the UI, and creating one needs a workspace
    to navigate from.
    """
    from core.tenancy.scope import tenant_scope

    allowed = await get_permission_service().check(
        ctx.tenant_id, ctx.principal_id, "manage_workspace", "workspace", workspace_id
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="manage_workspace required")

    async with tenant_scope(ctx.tenant_id) as session:
        row = await session.get(Workspace, workspace_id)
        if row is None or row.tenant_id != ctx.tenant_id or row.archived_at is not None:
            raise HTTPException(status_code=404, detail="no such workspace")
        remaining = await session.scalar(
            select(func.count())
            .select_from(Workspace)
            .where(
                Workspace.tenant_id == ctx.tenant_id,
                Workspace.archived_at.is_(None),
                Workspace.id != workspace_id,
            )
        )
        if not remaining:
            raise HTTPException(
                status_code=409,
                detail="this is your only workspace; create another one before archiving it",
            )
        row.archived_at = datetime.now(UTC)


class ScopeInfo(BaseModel):
    key: str
    kind: str
    member_count: int | None  # None for public/role (open-ended); a count for group/private


class PersonaVisibility(BaseModel):
    id: uuid.UUID
    key: str
    name: str
    persona_type: str
    scopes: list[str]  # scope keys this persona is entitled to read


class KnowledgeInScope(BaseModel):
    source_name: str
    class_: str = Field(serialization_alias="class")
    scope_key: str


class VisibilityMatrix(BaseModel):
    scopes: list[ScopeInfo]
    personas: list[PersonaVisibility]
    knowledge: list[KnowledgeInScope]


@router.get("/{workspace_id}/visibility")
async def get_workspace_visibility(
    workspace_id: uuid.UUID, ctx: RequestContext = Depends(get_request_context)
) -> VisibilityMatrix:
    """Who can read what. Each persona's entitled scope set is computed by the SAME
    ``scopes_for`` the assembler uses (via the EXPORT pseudo-phase = every scope the
    principal is entitled to), so this view can never drift from what actually reaches a
    turn's context. Read-only: this is the window onto who-knows-what, not the editor."""
    from core.assembler.models import ScopeRow
    from core.knowledge.models import KnowledgeSource, WorkspaceKnowledgeAttachment
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(ctx.tenant_id) as session:
        ws = await session.get(Workspace, workspace_id)
        if ws is None or ws.tenant_id != ctx.tenant_id or ws.archived_at is not None:
            raise HTTPException(status_code=404, detail="no such workspace")

        scope_rows = list(
            (
                await session.execute(select(ScopeRow).where(ScopeRow.workspace_id == workspace_id))
            ).scalars()
        )
        persona_rows = list(
            (
                await session.execute(select(Persona).where(Persona.workspace_id == workspace_id))
            ).scalars()
        )
        knowledge_rows = (
            await session.execute(
                select(
                    KnowledgeSource.name,
                    KnowledgeSource.class_,
                    WorkspaceKnowledgeAttachment.scope_key,
                )
                .join(
                    WorkspaceKnowledgeAttachment,
                    WorkspaceKnowledgeAttachment.knowledge_source_id == KnowledgeSource.id,
                )
                .where(WorkspaceKnowledgeAttachment.workspace_id == workspace_id)
                .order_by(WorkspaceKnowledgeAttachment.scope_key, KnowledgeSource.name)
            )
        ).all()

    def _member_count(row: ScopeRow) -> int | None:
        if row.kind in ("group", "private"):
            ids = row.members.get("principal_ids", [])
            return len(ids) if isinstance(ids, list) else 0
        return None  # public = everyone; role = anyone with the role, open-ended

    scopes = [
        ScopeInfo(key=r.key, kind=r.kind, member_count=_member_count(r))
        for r in sorted(scope_rows, key=lambda r: (r.kind != "public", r.key))
    ]

    personas: list[PersonaVisibility] = []
    for p in sorted(persona_rows, key=lambda p: p.name):
        entitled = await scopes_for(ctx.tenant_id, p.principal_id, workspace_id, EXPORT, None)
        keys = sorted(k for k in entitled if not k.startswith("agent_private:"))
        personas.append(
            PersonaVisibility(
                id=p.id, key=p.key, name=p.name, persona_type=p.persona_type, scopes=keys
            )
        )

    knowledge = [
        KnowledgeInScope(source_name=name, class_=class_, scope_key=scope_key)
        for name, class_, scope_key in knowledge_rows
    ]
    return VisibilityMatrix(scopes=scopes, personas=personas, knowledge=knowledge)


class PersonaScopesBody(BaseModel):
    # The GROUP scopes this persona should belong to. Public scopes (everyone) and role
    # scopes (by persona type) are not per-persona toggles and are ignored here; private
    # scopes are single-owner and not settable through a multiselect.
    scopes: list[str]


@router.put("/{workspace_id}/personas/{persona_id}/scopes")
async def set_persona_scopes(
    workspace_id: uuid.UUID,
    persona_id: uuid.UUID,
    body: PersonaScopesBody,
    ctx: RequestContext = Depends(get_request_context),
) -> PersonaVisibility:
    """Set which GROUP scopes a persona can read: add its principal to every listed group
    scope, remove it from the unlisted ones. Only ``group`` scopes are touched -- public
    and role membership is not a per-persona switch. The informational assistant is left
    alone: its grounding is re-scoped to the *requesting viewer* at answer time, so it
    needs no standing grants and must not carry them.

    Returns the persona's resulting entitlement, computed by the same ``scopes_for`` the
    assembler runs, so the caller sees the real effect rather than an echo of the request.
    """
    allowed = await get_permission_service().check(
        ctx.tenant_id, ctx.principal_id, "manage_workspace", "workspace", workspace_id
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="manage_workspace required")

    from core.assembler.models import ScopeRow
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(ctx.tenant_id) as session:
        persona = await session.get(Persona, persona_id)
        if persona is None or persona.workspace_id != workspace_id:
            raise HTTPException(status_code=404, detail="no such persona in this workspace")
        if persona.persona_type == "informational":
            raise HTTPException(
                status_code=409,
                detail="the assistant's visibility is scoped to each requesting viewer, "
                "not granted per scope; there is nothing to set here",
            )
        principal_id = str(persona.principal_id)
        want = set(body.scopes)
        group_scopes = list(
            (
                await session.execute(
                    select(ScopeRow).where(
                        ScopeRow.workspace_id == workspace_id, ScopeRow.kind == "group"
                    )
                )
            ).scalars()
        )
        for row in group_scopes:
            ids = row.members.get("principal_ids", [])
            ids = list(ids) if isinstance(ids, list) else []
            present = principal_id in ids
            if row.key in want and not present:
                ids.append(principal_id)
                row.members = {**row.members, "principal_ids": ids}
            elif row.key not in want and present:
                row.members = {
                    **row.members,
                    "principal_ids": [i for i in ids if i != principal_id],
                }
        principal_uuid = persona.principal_id
        p_id, p_key, p_name, p_type = persona.id, persona.key, persona.name, persona.persona_type

    entitled = await scopes_for(ctx.tenant_id, principal_uuid, workspace_id, EXPORT, None)
    keys = sorted(k for k in entitled if not k.startswith("agent_private:"))
    return PersonaVisibility(id=p_id, key=p_key, name=p_name, persona_type=p_type, scopes=keys)

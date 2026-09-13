"""Tenant + user lifecycle used by the admin console (Phase B).

Cross-tenant reads and the tenant row itself go through ``unscoped_session`` (the ``tenant``
table is not RLS-covered); per-tenant rows (principals, memberships, identities) go through
``tenant_scope``. Deactivation is a soft ``UPDATE`` -- a nullable timestamp -- so the app
role can do all of this without any superuser access; truly removing a tenant is the
separate ``core.tenancy.purge`` CLI's job.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select

# Workspace.vocabulary_overlay_id FKs to vocabulary_overlay.id by string reference, resolved
# only at mapper-configuration time -- VocabularyOverlayRow's module must be imported first,
# and core.tenancy.models can't do it itself (circular). Same guard core.tenancy.seed carries.
import core.vocabulary.models  # noqa: E402, F401
from core.assembler.visibility import seed_default_scopes
from core.tenancy.models import Identity, Membership, Principal, Tenant, Workspace
from core.tenancy.scope import tenant_scope, unscoped_session


class TenantExistsError(Exception):
    pass


@dataclass(frozen=True)
class TenantSummary:
    id: uuid.UUID
    slug: str
    name: str
    deactivated_at: datetime | None
    member_count: int
    agent_count: int
    default_overlay_key: str | None
    workflow_key: str | None


@dataclass(frozen=True)
class UserSummary:
    principal_id: uuid.UUID
    display_name: str
    role: str
    email: str | None
    disabled_at: datetime | None


async def list_tenants() -> list[TenantSummary]:
    """Every real tenant (the reserved library tenant is excluded), newest first, with cheap
    membership/agent counts for the console's overview."""
    async with unscoped_session() as session:
        tenants = (
            (
                await session.execute(
                    select(Tenant).where(Tenant.is_library.is_(False)).order_by(Tenant.created_at)
                )
            )
            .scalars()
            .all()
        )

    summaries: list[TenantSummary] = []
    for t in tenants:
        # Membership/Principal are RLS-covered: their counts must be read under a
        # tenant_scope for that tenant, not the unscoped session above (which sets no
        # app.tenant_id, so RLS would fail those queries safe to zero rows).
        async with tenant_scope(t.id) as scoped:
            members = await scoped.scalar(
                select(func.count()).select_from(Membership).where(Membership.tenant_id == t.id)
            )
            agents = await scoped.scalar(
                select(func.count())
                .select_from(Principal)
                .where(Principal.tenant_id == t.id, Principal.kind == "agent")
            )
        summaries.append(
            TenantSummary(
                id=t.id,
                slug=t.slug,
                name=t.name,
                deactivated_at=t.deactivated_at,
                member_count=int(members or 0),
                agent_count=int(agents or 0),
                default_overlay_key=_as_str(t.settings.get("default_vocabulary_overlay_key")),
                workflow_key=_as_str(t.settings.get("workflow_key")),
            )
        )
    return summaries


async def create_tenant(
    name: str,
    slug: str,
    *,
    workspace_key: str = "default",
    workspace_name: str = "Default Workspace",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a new tenant with a default workspace and the default visibility scopes.
    Raises ``TenantExistsError`` if the slug is taken. Users are added separately."""
    async with unscoped_session() as session:
        existing = await session.scalar(select(Tenant.id).where(Tenant.slug == slug))
        if existing is not None:
            raise TenantExistsError(f"tenant slug {slug!r} already exists")
        tenant = Tenant(slug=slug, name=name)
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id

    async with tenant_scope(tenant_id) as session:
        workspace = Workspace(tenant_id=tenant_id, key=workspace_key, name=workspace_name)
        session.add(workspace)
        await session.flush()
        workspace_id = workspace.id

    # Separate transaction on its own connection (matches seed_dev_tenant's ordering note:
    # seed_default_scopes' FK to workspace.id must see a committed row).
    await seed_default_scopes(tenant_id, workspace_id)
    return tenant_id, workspace_id


def _as_str(value: object) -> str | None:
    """A JSONB setting read back as text, or None if it is anything else."""
    return value if isinstance(value, str) else None


async def create_workspace(tenant_id: uuid.UUID, key: str, name: str) -> uuid.UUID:
    """A new workspace in an existing tenant, with the default visibility scopes --
    the self-serve counterpart of the workspace ``create_tenant`` seeds. The caller
    grants the creator's workspace membership (roles are the caller's policy)."""
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(Workspace.id).where(Workspace.tenant_id == tenant_id, Workspace.key == key)
        )
        if existing is not None:
            raise TenantExistsError(f"workspace key {key!r} already exists")
        workspace = Workspace(tenant_id=tenant_id, key=key, name=name)
        session.add(workspace)
        await session.flush()
        workspace_id = workspace.id
    await seed_default_scopes(tenant_id, workspace_id)
    # The tenant's workflow was pinned before this workspace existed, so its pack content
    # (entity schemas, process definitions -- the workspace-scoped kinds) was loaded into
    # the workspaces of the time. Without this catch-up the new workspace has no processes
    # to run at all. Best-effort: a missing pack must not fail workspace creation.
    try:
        from core.workflows.service import ensure_workflow_pack_for_workspace

        await ensure_workflow_pack_for_workspace(tenant_id, workspace_id)
    except Exception:  # noqa: BLE001 -- the workspace exists either way
        structlog.get_logger().warning(
            "workspace.workflow_pack_load_failed", workspace_id=str(workspace_id)
        )
    return workspace_id


async def list_tenant_workspace_ids(tenant_id: uuid.UUID) -> list[uuid.UUID]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
        ).scalars()
        return list(rows)


async def set_tenant_deactivated(tenant_id: uuid.UUID, deactivated: bool) -> None:
    """Soft toggle ``tenant.deactivated_at``. A deactivated tenant's principals cannot log in
    or make requests (the auth middleware rejects it). Reversible; never a delete."""
    async with unscoped_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise ValueError(f"no tenant {tenant_id}")
        tenant.deactivated_at = datetime.now(UTC) if deactivated else None


async def create_tenant_user(tenant_id: uuid.UUID, display_name: str, role: str) -> uuid.UUID:
    """Create a human principal + tenant membership, returning the principal id. The caller
    then attaches a login identity (email/password) via the IdentityProvider port -- the
    same two-step register path ``api.routes.auth.register`` uses."""
    async with tenant_scope(tenant_id) as session:
        principal = Principal(tenant_id=tenant_id, kind="human", display_name=display_name)
        session.add(principal)
        await session.flush()
        session.add(Membership(tenant_id=tenant_id, principal_id=principal.id, role=role))
        return principal.id


async def delete_principal(tenant_id: uuid.UUID, principal_id: uuid.UUID) -> None:
    """Remove a just-created principal whose identity creation lost a uniqueness race, so no
    dangling member with no way to authenticate is left behind (mirrors register's cleanup)."""
    async with tenant_scope(tenant_id) as session:
        principal = await session.get(Principal, principal_id)
        if principal is not None:
            await session.delete(principal)


async def list_tenant_users(tenant_id: uuid.UUID) -> list[UserSummary]:
    """Human principals in a tenant with their tenant role and (local) login email."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(
                    Principal.id,
                    Principal.display_name,
                    Principal.disabled_at,
                    Membership.role,
                    Identity.email,
                )
                .join(Membership, Membership.principal_id == Principal.id)
                .join(
                    Identity,
                    (Identity.principal_id == Principal.id) & (Identity.provider == "local"),
                    isouter=True,
                )
                .where(Principal.tenant_id == tenant_id, Principal.kind == "human")
                .order_by(Principal.created_at)
            )
        ).all()
        return [
            UserSummary(
                principal_id=pid,
                display_name=name,
                role=role,
                email=email,
                disabled_at=disabled_at,
            )
            for pid, name, disabled_at, role, email in rows
        ]


async def set_principal_disabled(
    tenant_id: uuid.UUID, principal_id: uuid.UUID, disabled: bool
) -> None:
    """Soft toggle ``principal.disabled_at`` -- a disabled principal is rejected by the auth
    middleware on every request. Reversible; never a delete."""
    async with tenant_scope(tenant_id) as session:
        principal = await session.get(Principal, principal_id)
        if principal is None:
            raise ValueError(f"no principal {principal_id}")
        principal.disabled_at = datetime.now(UTC) if disabled else None

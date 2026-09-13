"""E2.12's own acceptance-criteria tests for the `overseer.query` MCP tool: it must write
the same audit-row shape as the HTTP surface (same underlying `OverseerService.inspect()`
call, so one audit path, not two that could drift), and a token whose principal lacks
`secret:inspect` must be rejected before any read.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from adapters.encryptor.identity import IdentityEncryptor
from adapters.moderation.allow_all import AllowAllModerationProvider
from adapters.permission.role_permission import RolePermissionService
from api.mcp_server.tokens import issue_mcp_token
from api.mcp_server.tools.overseer_query import McpToolAuthError, overseer_query
from core.audit.models import AuditLogRow
from core.overseer.service import OverseerService
from core.secrets.authoring import create_secret
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope

_ENCRYPTOR = IdentityEncryptor()
_PERMISSIONS = RolePermissionService()
_MODERATION = AllowAllModerationProvider()


async def _grant_role(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, principal_id: uuid.UUID, role: str
) -> None:
    async with tenant_scope(tenant_id) as session:
        existing = await session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.principal_id == principal_id,
            )
        )
        if existing is not None:
            existing.role = role
            return
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal_id,
                role=role,
            )
        )


async def _seed_secret(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, owner_id: uuid.UUID
) -> uuid.UUID:
    row = await create_secret(
        tenant_id,
        workspace_id,
        owner_id,
        subject_kind="workspace",
        subject_id=workspace_id,
        content="the actual plaintext",
        gist="a one-liner gist",
        scope_key="workspace_public",
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
        moderation_provider=_MODERATION,
        behavioral_directive="act nervous",
    )
    return row.id


async def _audit_rows_for_action(tenant_id: uuid.UUID, action: str) -> list[AuditLogRow]:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(AuditLogRow)
                .where(AuditLogRow.tenant_id == tenant_id, AuditLogRow.action == action)
                .order_by(AuditLogRow.created_at)
            )
        ).scalars()
        return list(rows)


async def test_mcp_and_ui_inspections_write_identical_audit_shape(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    async with tenant_scope(tenant_a) as session:
        owner_id = (await session.execute(text("SELECT id FROM principal LIMIT 1"))).scalar_one()
        workspace_id = (
            await session.execute(text("SELECT id FROM workspace LIMIT 1"))
        ).scalar_one()
    await _grant_role(tenant_a, workspace_id, owner_id, "facilitator")
    secret_id = await _seed_secret(tenant_a, workspace_id, owner_id)
    await _grant_role(tenant_a, workspace_id, owner_id, "overseer")

    # The "UI" side: exactly what packages/api/overseer/routes.py's inspect_secret does.
    ui_service = OverseerService(encryptor=_ENCRYPTOR, permission_service=_PERMISSIONS)
    ui_view = await ui_service.inspect(
        tenant_a,
        owner_id,
        workspace_id,
        secret_id,
        query={"endpoint": "GET /overseer/secrets/{secret_id}"},
    )
    assert ui_view.content == "the actual plaintext"

    # The MCP side: same principal, same secret, through the tool instead.
    token = issue_mcp_token(tenant_id=tenant_a, workspace_id=workspace_id, principal_id=owner_id)
    mcp_result = await overseer_query(
        token, secret_id, encryptor=_ENCRYPTOR, permission_service=_PERMISSIONS
    )
    assert mcp_result.content == "the actual plaintext"

    rows = await _audit_rows_for_action(tenant_a, "secret:inspect")
    assert len(rows) == 2
    ui_row, mcp_row = rows

    # Identical shape apart from the query's own "surface" tag -- one audit path
    # underneath both surfaces, not two that could quietly drift apart.
    assert ui_row.actor_principal_id == mcp_row.actor_principal_id == owner_id
    assert ui_row.resource_type == mcp_row.resource_type == "secret"
    assert ui_row.resource_id == mcp_row.resource_id == secret_id
    assert ui_row.target_ids == mcp_row.target_ids == [secret_id]
    assert ui_row.action == mcp_row.action == "secret:inspect"
    assert mcp_row.query is not None and mcp_row.query.get("surface") == "mcp"
    assert ui_row.query is not None and ui_row.query.get("surface") != "mcp"


async def test_mcp_token_without_inspect_permission_reads_nothing(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    async with tenant_scope(tenant_a) as session:
        owner_id = (await session.execute(text("SELECT id FROM principal LIMIT 1"))).scalar_one()
        workspace_id = (
            await session.execute(text("SELECT id FROM workspace LIMIT 1"))
        ).scalar_one()
    await _grant_role(tenant_a, workspace_id, owner_id, "facilitator")
    secret_id = await _seed_secret(tenant_a, workspace_id, owner_id)

    async with tenant_scope(tenant_a) as session:
        outsider = Principal(tenant_id=tenant_a, kind="agent", display_name="Untrusted MCP client")
        session.add(outsider)
        await session.flush()
        outsider_id = outsider.id
    # No WorkspaceMembership for `outsider` at all -- no role, therefore no secret:inspect.

    token = issue_mcp_token(tenant_id=tenant_a, workspace_id=workspace_id, principal_id=outsider_id)

    before = await _audit_rows_for_action(tenant_a, "secret:inspect")

    with pytest.raises(McpToolAuthError):
        await overseer_query(
            token, secret_id, encryptor=_ENCRYPTOR, permission_service=_PERMISSIONS
        )

    after = await _audit_rows_for_action(tenant_a, "secret:inspect")
    # Denied before the transaction that would read+audit ever opened.
    assert after == before

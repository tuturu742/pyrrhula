"""Tests for `OverseerService` and the designated-overseer product rule. INV-1's allowlist
itself is covered generically by `tests/architecture/test_inv1_import_graph.py` -- this
module only needs to prove the service's own atomicity, permission-gating, and
chain-tamper-detection claims, plus the workspace-activation rule.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from adapters.encryptor.identity import IdentityEncryptor
from adapters.moderation.allow_all import AllowAllModerationProvider
from adapters.permission.role_permission import RolePermissionService
from core.audit.models import AuditLogRow
from core.audit.service import AuditService
from core.audit.verify import verify_chain
from core.config import get_settings
from core.overseer.service import (
    OverseerPermissionDeniedError,
    OverseerService,
    SecretNotFoundError,
)
from core.overseer.workspace_requirements import (
    OverseerRequiredError,
    validate_overseer_requirement,
)
from core.secrets.authoring import create_secret
from core.tenancy.models import Principal, WorkspaceMembership
from core.tenancy.scope import tenant_scope

_ENCRYPTOR = IdentityEncryptor()
_PERMISSIONS = RolePermissionService()
_MODERATION = AllowAllModerationProvider()


class _RaisingAuditService(AuditService):
    """Simulates a forced failure between the read and the audit append -- the only way
    to force that specific window to fail without touching the real hash-chain logic."""

    async def append_in_session(self, session: AsyncSession, **kwargs: object) -> AuditLogRow:
        raise RuntimeError("forced failure between read and append")


async def _grant_role(
    tenant_id: uuid.UUID, workspace_id: uuid.UUID, principal_id: uuid.UUID, role: str
) -> None:
    """Upserts the membership's role -- a second call for the same (workspace,
    principal) with a different role promotes/demotes it rather than being a no-op, so
    tests can seed a plain membership and later grant `overseer` on top of it."""
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
    )
    return row.id


async def test_inspect_read_and_audit_row_are_atomic(
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

    working_service = OverseerService(encryptor=_ENCRYPTOR, permission_service=_PERMISSIONS)
    view = await working_service.inspect(tenant_a, owner_id, workspace_id, secret_id)
    assert view.content == "the actual plaintext"

    async with tenant_scope(tenant_a) as session:
        before = (
            await session.execute(
                select(AuditLogRow.id).where(AuditLogRow.action == "secret:inspect")
            )
        ).all()
    assert len(before) == 1

    broken_service = OverseerService(
        encryptor=_ENCRYPTOR,
        permission_service=_PERMISSIONS,
        audit_service=_RaisingAuditService(),
    )
    with pytest.raises(RuntimeError, match="forced failure"):
        await broken_service.inspect(tenant_a, owner_id, workspace_id, secret_id)

    async with tenant_scope(tenant_a) as session:
        after = (
            await session.execute(
                select(AuditLogRow.id).where(AuditLogRow.action == "secret:inspect")
            )
        ).all()
    # The forced failure left no new audit row -- the caller also never received a
    # SecretView (the exception propagated instead of returning one), so neither the
    # read nor the audit row is observable from that failed call.
    assert len(after) == len(before)


async def test_inspect_is_gated_by_permission_not_by_scope(
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
        outsider = Principal(tenant_id=tenant_a, kind="human", display_name="Outsider")
        session.add(outsider)
        await session.flush()
        outsider_id = outsider.id
    # Deliberately no WorkspaceMembership at all for `outsider` -- no role, therefore no
    # secret:inspect grant, regardless of any in-fiction scope_key on the secret itself.

    service = OverseerService(encryptor=_ENCRYPTOR, permission_service=_PERMISSIONS)
    with pytest.raises(OverseerPermissionDeniedError):
        await service.inspect(tenant_a, outsider_id, workspace_id, secret_id)

    async with tenant_scope(tenant_a) as session:
        rows = (
            await session.execute(
                select(AuditLogRow.id).where(AuditLogRow.action == "secret:inspect")
            )
        ).all()
    # Denied before the transaction that would read+audit ever opened -- no audit-free
    # partial data, and no audit row for a call that never actually read anything either.
    assert rows == []


async def test_verifier_detects_tampered_inspection_audit_row(
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

    service = OverseerService(encryptor=_ENCRYPTOR, permission_service=_PERMISSIONS)
    await service.inspect(tenant_a, owner_id, workspace_id, secret_id)
    view = await service.inspect(tenant_a, owner_id, workspace_id, secret_id)
    assert view.content == "the actual plaintext"

    async with tenant_scope(tenant_a) as session:
        inspect_row_id = await session.scalar(
            select(AuditLogRow.id)
            .where(AuditLogRow.action == "secret:inspect")
            .order_by(AuditLogRow.created_at.desc(), AuditLogRow.id.desc())
            .limit(1)
        )

    admin_engine = create_async_engine(get_settings().database_url)
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("UPDATE audit_log SET action = 'tampered' WHERE id = :id"),
                {"id": inspect_row_id},
            )
    finally:
        await admin_engine.dispose()

    broken = await verify_chain(tenant_a)
    assert inspect_row_id in broken


async def test_multi_human_workspace_requires_designated_overseer(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    async with tenant_scope(tenant_a) as session:
        solo_owner_id = (
            await session.execute(text("SELECT id FROM principal LIMIT 1"))
        ).scalar_one()
        workspace_id = (
            await session.execute(text("SELECT id FROM workspace LIMIT 1"))
        ).scalar_one()

    # Solo-human workspace (just the seeded owner): no overseer required.
    await validate_overseer_requirement(tenant_a, workspace_id)

    async with tenant_scope(tenant_a) as session:
        second_human = Principal(tenant_id=tenant_a, kind="human", display_name="Second Human")
        session.add(second_human)
        await session.flush()
        second_human_id = second_human.id
    await _grant_role(tenant_a, workspace_id, solo_owner_id, "participant")
    await _grant_role(tenant_a, workspace_id, second_human_id, "participant")

    with pytest.raises(OverseerRequiredError):
        await validate_overseer_requirement(tenant_a, workspace_id)

    await _grant_role(tenant_a, workspace_id, solo_owner_id, "overseer")
    await validate_overseer_requirement(tenant_a, workspace_id)


async def test_inspect_missing_secret_raises_not_found(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _tenant_b = two_tenants
    async with tenant_scope(tenant_a) as session:
        owner_id = (await session.execute(text("SELECT id FROM principal LIMIT 1"))).scalar_one()
        workspace_id = (
            await session.execute(text("SELECT id FROM workspace LIMIT 1"))
        ).scalar_one()
    await _grant_role(tenant_a, workspace_id, owner_id, "overseer")

    service = OverseerService(encryptor=_ENCRYPTOR, permission_service=_PERMISSIONS)
    with pytest.raises(SecretNotFoundError):
        await service.inspect(tenant_a, owner_id, workspace_id, uuid.uuid4())

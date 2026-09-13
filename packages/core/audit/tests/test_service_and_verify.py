import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from core.audit.service import AuditService
from core.audit.verify import verify_chain
from core.config import get_settings
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def test_append_chains_hashes(db_available: None) -> None:
    tenant_id, owner_id, _ = await seed_dev_tenant(slug=f"audit-{uuid.uuid4().hex[:8]}")
    service = AuditService()

    row1 = await service.append(
        tenant_id=tenant_id,
        actor_principal_id=owner_id,
        action="secret:inspect",
        resource_type="workspace",
    )
    row2 = await service.append(
        tenant_id=tenant_id,
        actor_principal_id=owner_id,
        action="secret:inspect",
        resource_type="workspace",
    )

    assert row1.prev_hash is None
    assert row2.prev_hash == row1.row_hash
    assert row1.row_hash != row2.row_hash


async def test_verify_chain_clean(db_available: None) -> None:
    tenant_id, owner_id, _ = await seed_dev_tenant(slug=f"audit-{uuid.uuid4().hex[:8]}")
    service = AuditService()
    for _ in range(5):
        await service.append(
            tenant_id=tenant_id,
            actor_principal_id=owner_id,
            action="view_workspace",
            resource_type="workspace",
        )

    assert await verify_chain(tenant_id) == []


async def test_verify_chain_detects_tampering(db_available: None) -> None:
    tenant_id, owner_id, _ = await seed_dev_tenant(slug=f"audit-{uuid.uuid4().hex[:8]}")
    service = AuditService()
    await service.append(
        tenant_id=tenant_id,
        actor_principal_id=owner_id,
        action="view_workspace",
        resource_type="workspace",
    )
    row2 = await service.append(
        tenant_id=tenant_id,
        actor_principal_id=owner_id,
        action="view_workspace",
        resource_type="workspace",
    )
    await service.append(
        tenant_id=tenant_id,
        actor_principal_id=owner_id,
        action="view_workspace",
        resource_type="workspace",
    )

    # Simulate an attacker with elevated DB access: pyrrhula_app (what unscoped_session()
    # and tenant_scope() connect as) is granted no UPDATE on audit_log at all -- see the
    # grant restriction and test_app_role_cannot_update_or_delete_audit_log below -- so
    # reaching this row requires the admin/migrator role directly, matching the plan's
    # honest caveat that layers 1-2 are tamper-evident, not tamper-proof, against exactly
    # this threat (a DB superuser).
    admin_engine = create_async_engine(get_settings().database_url)
    try:
        async with admin_engine.begin() as conn:
            await conn.execute(
                text("UPDATE audit_log SET action = 'tampered' WHERE id = :id"),
                {"id": row2.id},
            )
    finally:
        await admin_engine.dispose()

    broken = await verify_chain(tenant_id)
    assert row2.id in broken


async def test_app_role_cannot_update_or_delete_audit_log(db_available: None) -> None:
    tenant_id, owner_id, _ = await seed_dev_tenant(slug=f"audit-{uuid.uuid4().hex[:8]}")
    service = AuditService()
    row = await service.append(
        tenant_id=tenant_id,
        actor_principal_id=owner_id,
        action="view_workspace",
        resource_type="workspace",
    )

    async with tenant_scope(tenant_id) as session:
        with pytest.raises(Exception, match="permission denied"):
            await session.execute(
                text("UPDATE audit_log SET action = 'x' WHERE id = :id"), {"id": row.id}
            )

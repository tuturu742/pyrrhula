import uuid

import pytest

from adapters.permission.role_permission import RolePermissionService
from core.ports.permission import PermissionService, UnknownActionError
from core.tenancy.seed import seed_dev_tenant


async def test_owner_has_manage_tenant(db_available: None) -> None:
    tenant_id, owner_id, _ = await seed_dev_tenant(slug=f"perm-{uuid.uuid4().hex[:8]}")
    service: PermissionService = RolePermissionService()

    assert await service.check(tenant_id, owner_id, "manage_tenant", "tenant", tenant_id) is True


async def test_stranger_lacks_manage_tenant(db_available: None) -> None:
    tenant_id, _owner_id, _ = await seed_dev_tenant(slug=f"perm-{uuid.uuid4().hex[:8]}")
    service: PermissionService = RolePermissionService()

    stranger_id = uuid.uuid4()
    assert (
        await service.check(tenant_id, stranger_id, "manage_tenant", "tenant", tenant_id) is False
    )


async def test_facilitator_can_act_in_session_in_own_workspace(db_available: None) -> None:
    tenant_id, owner_id, workspace_id = await seed_dev_tenant(slug=f"perm-{uuid.uuid4().hex[:8]}")
    service: PermissionService = RolePermissionService()

    # seed_dev_tenant only creates a tenant-level owner membership; grant a workspace role
    # directly for this test.
    from core.tenancy.models import WorkspaceMembership
    from core.tenancy.scope import tenant_scope

    async with tenant_scope(tenant_id) as session:
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=owner_id,
                role="facilitator",
            )
        )

    assert (
        await service.check(tenant_id, owner_id, "act_in_session", "workspace", workspace_id)
        is True
    )


async def test_unknown_action_raises(db_available: None) -> None:
    tenant_id, owner_id, _ = await seed_dev_tenant(slug=f"perm-{uuid.uuid4().hex[:8]}")
    service: PermissionService = RolePermissionService()

    with pytest.raises(UnknownActionError):
        await service.check(tenant_id, owner_id, "reticulate_splines", "tenant", tenant_id)

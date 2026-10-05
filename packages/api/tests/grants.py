"""Role grants for the api flow tests.

``/auth/register`` hands out the least privilege there is -- a tenant ``viewer`` with no
workspace seat -- which is the right default for self-service and the wrong one for a
test that then authors knowledge, conducts a session or manages a workspace. These two
helpers give a registered user the seat such a test needs, writing the same rows an
owner would through ``/tenant/users`` and ``/workspaces/{id}/members``. A test that
wants a refusal registers and grants nothing.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import update

from core.tenancy.models import Membership, WorkspaceMembership
from core.tenancy.scope import tenant_scope


def principal_of(client: TestClient, token: str) -> uuid.UUID:
    me = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    return uuid.UUID(me.json()["principal_id"])


async def grant_tenant_role(
    client: TestClient, token: str, tenant_id: uuid.UUID, role: str
) -> uuid.UUID:
    """Replace the registrant's tenant membership role (owner|admin|editor|participant|viewer)."""
    principal_id = principal_of(client, token)
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            update(Membership)
            .where(Membership.tenant_id == tenant_id, Membership.principal_id == principal_id)
            .values(role=role)
        )
    return principal_id


async def grant_workspace_role(
    client: TestClient,
    token: str,
    tenant_id: uuid.UUID,
    workspace_id: uuid.UUID,
    role: str = "facilitator",
) -> uuid.UUID:
    """Seat the registrant in a workspace (steward|facilitator|participant|overseer|viewer)."""
    principal_id = principal_of(client, token)
    async with tenant_scope(tenant_id) as session:
        session.add(
            WorkspaceMembership(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                principal_id=principal_id,
                role=role,
            )
        )
    return principal_id

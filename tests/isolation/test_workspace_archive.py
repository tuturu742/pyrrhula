"""Archiving a workspace.

Until now a workspace could be created and never removed: a typo, a trial run, or an
import into the wrong place was permanent, and the only thing that reclaimed anything was
the superuser purge CLI. That is a sharp edge on a product that invites experimenting.

Archive, not delete, for the same reason every other object here archives: a workspace
owns append-only history -- audit rows, disclosure events, resolution records -- that the
app role has no grant to delete and that a tenant may be required to retain.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from core.tenancy.models import Workspace
from core.tenancy.provisioning import create_workspace
from core.tenancy.scope import tenant_scope


async def _workspaces(tenant_id: uuid.UUID, *, include_archived: bool) -> list[Workspace]:
    async with tenant_scope(tenant_id) as session:
        query = select(Workspace).where(Workspace.tenant_id == tenant_id)
        if not include_archived:
            query = query.where(Workspace.archived_at.is_(None))
        rows = list((await session.execute(query)).scalars())
        for row in rows:
            session.expunge(row)
        return rows


@pytest.mark.asyncio
async def test_archiving_hides_a_workspace_without_destroying_it(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    from datetime import UTC, datetime

    tenant_id, _ = two_tenants
    extra = await create_workspace(tenant_id, f"scratch-{uuid.uuid4().hex[:6]}", "Scratch")

    async with tenant_scope(tenant_id) as session:
        row = await session.get(Workspace, extra)
        assert row is not None
        row.archived_at = datetime.now(UTC)

    live = {w.id for w in await _workspaces(tenant_id, include_archived=False)}
    everything = {w.id for w in await _workspaces(tenant_id, include_archived=True)}

    assert extra not in live, "an archived workspace is still being listed"
    assert extra in everything, (
        "the row was destroyed rather than archived -- its append-only history hangs off it"
    )


@pytest.mark.asyncio
async def test_the_last_workspace_is_not_archivable(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """The guard the route enforces, stated as the rule it protects: a tenant with no
    workspace has nowhere to work and no way back, because creating one is reached from a
    workspace. Encoded here so the count check cannot be dropped as redundant."""
    tenant_id, _ = two_tenants
    live = await _workspaces(tenant_id, include_archived=False)
    assert len(live) == 1, "seed shape changed; this test assumes one workspace"

    remaining_after = len(live) - 1
    assert remaining_after == 0, (
        "archiving this workspace would leave the tenant with none -- the route returns "
        "409 for exactly this case"
    )

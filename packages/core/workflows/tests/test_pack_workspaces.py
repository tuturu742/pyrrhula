"""A workflow's pack content has to reach every workspace of the tenant.

A workflow is pinned per tenant, so every workspace under it runs that workflow -- but
entity schemas and process definitions are workspace-scoped rows. The loader used to write
them into ``limit(1)`` with no ORDER BY: one arbitrary workspace. The others had no
processes at all, so a session there could only use whatever arrived with an import --
observed as a workspace whose only flow was a bundle's single-phase one while the pack's
facilitator-led flow sat in a sibling workspace.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from core.plugins.service import ensure_default_synced
from core.process.models import ProcessDefinitionRow
from core.tenancy.provisioning import create_workspace
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant
from core.workflows.service import set_tenant_workflow

pytestmark = pytest.mark.asyncio


async def _process_keys(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> set[str]:
    async with tenant_scope(tenant_id) as session:
        rows = await session.execute(
            select(ProcessDefinitionRow.key).where(
                ProcessDefinitionRow.workspace_id == workspace_id
            )
        )
        return {k for (k,) in rows}


async def _process_count(tenant_id: uuid.UUID, workspace_id: uuid.UUID) -> int:
    async with tenant_scope(tenant_id) as session:
        rows = await session.execute(
            select(ProcessDefinitionRow.id).where(ProcessDefinitionRow.workspace_id == workspace_id)
        )
        return len(rows.all())


async def test_every_existing_workspace_gets_the_pack(db_available: None) -> None:
    await ensure_default_synced()
    tenant_id, _owner, first_ws = await seed_dev_tenant(slug=f"wfpack-{uuid.uuid4().hex[:8]}")
    second_ws = await create_workspace(tenant_id, "second", "Second")

    await set_tenant_workflow(tenant_id, "rpg")

    for ws in (first_ws, second_ws):
        assert "standard_session_flow" in await _process_keys(tenant_id, ws), (
            f"workspace {ws} has no pack processes"
        )


async def test_a_workspace_created_later_catches_up(db_available: None) -> None:
    """The pack was loaded into the workspaces that existed at selection time; a new one
    must not be left with nothing to run."""
    await ensure_default_synced()
    tenant_id, _owner, _first = await seed_dev_tenant(slug=f"wfpack-{uuid.uuid4().hex[:8]}")
    await set_tenant_workflow(tenant_id, "rpg")

    later = await create_workspace(tenant_id, "later", "Later")

    assert "standard_session_flow" in await _process_keys(tenant_id, later)


async def test_reselecting_the_same_workflow_does_not_reload(db_available: None) -> None:
    """The stamp is what stops the loader version-bumping definitions on every reselect."""
    await ensure_default_synced()
    tenant_id, _owner, ws = await seed_dev_tenant(slug=f"wfpack-{uuid.uuid4().hex[:8]}")
    await set_tenant_workflow(tenant_id, "rpg")
    before = await _process_count(tenant_id, ws)

    await set_tenant_workflow(tenant_id, "rpg")

    assert await _process_count(tenant_id, ws) == before, (
        "reselecting the same workflow duplicated pack content"
    )

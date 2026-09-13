"""T0.4 coverage for ``persona_version`` (F3.12): registered in
``test_coverage_guard.py``'s ``_COVERED_TABLES``. Functional coverage for
``core.agents.editing`` (propose/apply persona edits) lives in
``packages/core/agents/tests/test_editing.py``; this file only proves the cross-tenant
filter-omission property, which needs the ``two_tenants`` fixture that's only available
under this directory's own conftest."""

from __future__ import annotations

import uuid

from sqlalchemy import select, text

from adapters.encryptor.identity import IdentityEncryptor
from core.agents.authoring import create_agent, create_persona, record_persona_version
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope


async def test_persona_version_cross_tenant_filter_omission_returns_zero_rows(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """A raw query with no ``WHERE tenant_id`` at all, scoped to tenant A via
    ``tenant_scope()``, must never surface tenant B's rows -- proving RLS is doing the
    filtering, not the query."""
    tenant_a, tenant_b = two_tenants

    for tenant_id in (tenant_a, tenant_b):
        async with tenant_scope(tenant_id) as session:
            workspace_id = (
                await session.execute(select(Workspace.id).where(Workspace.tenant_id == tenant_id))
            ).scalar_one()
        profile = await create_agent(
            tenant_id, "cross-tenant-test", "echo", "echo-1", encryptor=IdentityEncryptor()
        )
        agent = await create_persona(tenant_id, workspace_id, "narrator", "Narrator", profile.id)
        await record_persona_version(
            tenant_id, agent.id, "a persona", created_by=None, ai_assisted=False
        )

    async with tenant_scope(tenant_a) as session:
        rows = (await session.execute(text("SELECT tenant_id FROM persona_version"))).all()

    assert {row[0] for row in rows} == {tenant_a}

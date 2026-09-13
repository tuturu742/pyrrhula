"""Repo registry + session repo selection: RLS isolation (rule 4).

``repo`` and ``session_repo`` are standard tenant-scoped tables (RLS FORCE, the NULLIF
policy shape). These are their explicit filter-omission tests: another tenant cannot see,
select, or archive a repo -- even via raw, filter-omitting SQL -- and a session's repo
roster is invisible across tenants.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from core.agents.seed import seed_dev_agent
from core.process.skeleton import create_session
from core.repos.service import (
    RepoNotFoundError,
    archive_repo,
    create_repo,
    list_repos,
    list_session_repos,
    set_session_repos,
)
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope

pytestmark = pytest.mark.asyncio


async def _session_for(tenant_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        workspace_id = await session.scalar(
            select(Workspace.id).where(Workspace.tenant_id == tenant_id)
        )
    assert workspace_id is not None
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    sess = await create_session(tenant_id, workspace_id, persona_id)
    return sess.id


async def test_repo_invisible_to_other_tenant(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    repo = await create_repo(tenant_a, "proj-x", "Project X", runtime="node20")

    assert [r.key for r in await list_repos(tenant_a)] == ["proj-x"]
    assert await list_repos(tenant_b) == []

    async with tenant_scope(tenant_b) as session:
        leaked = await session.scalar(text("SELECT count(*) FROM repo WHERE key = 'proj-x'"))
    assert leaked == 0

    with pytest.raises(RepoNotFoundError):
        await archive_repo(tenant_b, repo.id)


async def test_session_repo_selection_is_tenant_scoped(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    repo = await create_repo(tenant_a, "proj-y", "Project Y")

    session_a = await _session_for(tenant_a)
    await set_session_repos(tenant_a, session_a, [repo.id])

    assert [r.key for r in await list_session_repos(tenant_a, session_a)] == ["proj-y"]
    # The other tenant sees nothing for the same session id, even raw.
    assert await list_session_repos(tenant_b, session_a) == []
    async with tenant_scope(tenant_b) as session:
        leaked = await session.scalar(
            text("SELECT count(*) FROM session_repo WHERE session_id = :sid"),
            {"sid": str(session_a)},
        )
    assert leaked == 0

    # Cross-tenant selection is refused: B cannot pin A's repo onto its own session.
    session_b = await _session_for(tenant_b)
    with pytest.raises(RepoNotFoundError):
        await set_session_repos(tenant_b, session_b, [repo.id])


async def test_archived_repo_cannot_be_selected(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    repo = await create_repo(tenant_a, "proj-z", "Project Z")
    await archive_repo(tenant_a, repo.id)
    assert await list_repos(tenant_a) == []

    session_a = await _session_for(tenant_a)
    with pytest.raises(RepoNotFoundError):
        await set_session_repos(tenant_a, session_a, [repo.id])

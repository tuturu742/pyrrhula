"""Per-persona hosted-git identity (G4.17): binding, rotation, fallback.

The table and ``resolve_git_identity`` shipped without any write path, so the resolver's
persona branch was never exercised -- every persona acted under the repo's one identity,
which is exactly what stops a reviewer bot approving a pull request another bot opened.
These cover the branch now that it can be written.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from core.agents.seed import seed_dev_agent
from core.repos.service import (
    InvalidRepoError,
    bind_persona_credential,
    create_repo,
    list_persona_credentials,
    resolve_git_identity,
    unbind_persona_credential,
)
from core.tenancy.models import Workspace
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant

pytestmark = pytest.mark.asyncio


async def _fixture() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """tenant, repo, persona, and the repo's own default credential ref."""
    tenant_id, _owner, _ws = await seed_dev_tenant(slug=f"pgc-{uuid.uuid4().hex[:8]}")
    async with tenant_scope(tenant_id) as session:
        workspace_id = await session.scalar(
            select(Workspace.id).where(Workspace.tenant_id == tenant_id)
        )
    assert workspace_id is not None
    persona_id = await seed_dev_agent(tenant_id, workspace_id)
    repo_default = uuid.uuid4()  # stands in for a sealed provider_credential id
    repo = await create_repo(
        tenant_id,
        f"r-{uuid.uuid4().hex[:8]}",
        "Repo",
        credential_ref=repo_default,
    )
    return tenant_id, repo.id, persona_id, repo_default


async def test_without_a_binding_a_persona_acts_as_the_repo(db_available: None) -> None:
    tenant_id, repo_id, persona_id, repo_default = await _fixture()
    assert await resolve_git_identity(tenant_id, repo_id, persona_id) == repo_default


async def test_a_binding_wins_over_the_repo_default(db_available: None) -> None:
    tenant_id, repo_id, persona_id, repo_default = await _fixture()
    own = uuid.uuid4()

    await bind_persona_credential(tenant_id, repo_id, persona_id, own)

    assert await resolve_git_identity(tenant_id, repo_id, persona_id) == own
    # A different persona, and the no-persona case, still fall back.
    assert await resolve_git_identity(tenant_id, repo_id, uuid.uuid4()) == repo_default
    assert await resolve_git_identity(tenant_id, repo_id, None) == repo_default


async def test_rebinding_rotates_instead_of_colliding(db_available: None) -> None:
    """(repo_id, persona_id) is unique, so a re-run of a setup script must update the
    row rather than raise on the constraint."""
    tenant_id, repo_id, persona_id, _default = await _fixture()
    first, second = uuid.uuid4(), uuid.uuid4()

    await bind_persona_credential(tenant_id, repo_id, persona_id, first)
    await bind_persona_credential(tenant_id, repo_id, persona_id, second)

    assert await resolve_git_identity(tenant_id, repo_id, persona_id) == second
    assert len(await list_persona_credentials(tenant_id, repo_id)) == 1


async def test_unbinding_falls_back_to_the_repo(db_available: None) -> None:
    tenant_id, repo_id, persona_id, repo_default = await _fixture()
    await bind_persona_credential(tenant_id, repo_id, persona_id, uuid.uuid4())

    assert await unbind_persona_credential(tenant_id, repo_id, persona_id) is True
    assert await resolve_git_identity(tenant_id, repo_id, persona_id) == repo_default
    # Idempotent: unbinding again reports that there was nothing to remove.
    assert await unbind_persona_credential(tenant_id, repo_id, persona_id) is False


async def test_binding_against_an_unknown_repo_is_refused(db_available: None) -> None:
    tenant_id, _repo_id, persona_id, _default = await _fixture()
    with pytest.raises(InvalidRepoError):
        await bind_persona_credential(tenant_id, uuid.uuid4(), persona_id, uuid.uuid4())


async def test_bindings_are_invisible_across_tenants(db_available: None) -> None:
    """RLS: another tenant must not see this repo's bindings at all."""
    tenant_a, repo_a, persona_a, _d = await _fixture()
    tenant_b, _repo_b, _persona_b, _d2 = await _fixture()

    await bind_persona_credential(tenant_a, repo_a, persona_a, uuid.uuid4())

    assert len(await list_persona_credentials(tenant_a, repo_a)) == 1
    assert await list_persona_credentials(tenant_b, repo_a) == []

"""A1.10 unit coverage for the library tenant helpers, against a live Postgres:
fork-on-edit forks a library source into the caller's tenant, reuses an already-forked
copy on a second edit rather than forking again, and leaves a non-library source alone.
"""

from __future__ import annotations

import uuid

import pytest

from core.knowledge.authoring import EntryFields, create_source, publish_version, upsert_draft_entry
from core.knowledge.library import (
    LIBRARY_TENANT_ID,
    fork_if_library,
    is_library_source,
    seed_library_source,
)
from core.knowledge.models import KnowledgeSource
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


def _entry(title: str = "Entry") -> EntryFields:
    return EntryFields(title=title, body_md="body", class_="rules", scope_key="workspace_public")


async def _seed_library_source(key: str) -> uuid.UUID:
    source = await create_source(LIBRARY_TENANT_ID, key=key, name=key, class_="rules")
    await upsert_draft_entry(LIBRARY_TENANT_ID, source.id, "entry-a", _entry())
    await publish_version(LIBRARY_TENANT_ID, source.id)
    return source.id


async def test_is_library_source_true_for_a_library_owned_source(db_available: None) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"lib-check-{uuid.uuid4().hex[:8]}"
    )
    library_source_id = await _seed_library_source(f"lib-{uuid.uuid4().hex[:8]}")

    assert await is_library_source(tenant_id, library_source_id) is True


async def test_is_library_source_false_for_a_tenants_own_source(db_available: None) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"lib-check-own-{uuid.uuid4().hex[:8]}"
    )
    own_source = await create_source(tenant_id, key="own", name="Own", class_="rules")

    assert await is_library_source(tenant_id, own_source.id) is False


async def test_fork_if_library_returns_the_source_id_unchanged_when_not_a_library_source(
    db_available: None,
) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"lib-noop-{uuid.uuid4().hex[:8]}"
    )
    own_source = await create_source(tenant_id, key="own", name="Own", class_="rules")

    result = await fork_if_library(tenant_id, own_source.id)

    assert result == own_source.id


async def test_fork_if_library_forks_a_library_source_into_the_caller_tenant(
    db_available: None,
) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"lib-fork-{uuid.uuid4().hex[:8]}"
    )
    library_source_id = await _seed_library_source(f"lib-{uuid.uuid4().hex[:8]}")

    forked_id = await fork_if_library(tenant_id, library_source_id)

    assert forked_id != library_source_id
    async with tenant_scope(tenant_id) as session:
        forked = await session.get(KnowledgeSource, forked_id)
    assert forked is not None
    assert forked.tenant_id == tenant_id


async def test_fork_if_library_reuses_an_existing_fork_on_a_second_edit(
    db_available: None,
) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"lib-reuse-{uuid.uuid4().hex[:8]}"
    )
    library_source_id = await _seed_library_source(f"lib-{uuid.uuid4().hex[:8]}")

    first_fork_id = await fork_if_library(tenant_id, library_source_id)
    second_fork_id = await fork_if_library(tenant_id, library_source_id)

    assert first_fork_id == second_fork_id


async def test_fork_if_library_gives_two_different_tenants_two_different_forks(
    db_available: None,
) -> None:
    tenant_a, _, _ = await seed_dev_tenant(slug=f"lib-fork-a-{uuid.uuid4().hex[:8]}")
    tenant_b, _, _ = await seed_dev_tenant(slug=f"lib-fork-b-{uuid.uuid4().hex[:8]}")
    library_source_id = await _seed_library_source(f"lib-{uuid.uuid4().hex[:8]}")

    fork_a = await fork_if_library(tenant_a, library_source_id)
    fork_b = await fork_if_library(tenant_b, library_source_id)

    assert fork_a != fork_b
    async with tenant_scope(tenant_a) as session:
        assert (await session.get(KnowledgeSource, fork_a)).tenant_id == tenant_a
    async with tenant_scope(tenant_b) as session:
        assert (await session.get(KnowledgeSource, fork_b)).tenant_id == tenant_b


async def test_fork_if_library_raises_for_an_unpublished_library_source(
    db_available: None,
) -> None:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"lib-unpub-{uuid.uuid4().hex[:8]}"
    )
    unpublished = await create_source(
        LIBRARY_TENANT_ID, key=f"unpub-{uuid.uuid4().hex[:8]}", name="Unpub", class_="rules"
    )

    with pytest.raises(ValueError, match="no published version"):
        await fork_if_library(tenant_id, unpublished.id)


async def test_seed_library_source_publishes_content_under_the_library_tenant(
    db_available: None,
) -> None:
    source = await seed_library_source(
        key=f"seed-{uuid.uuid4().hex[:8]}",
        name="Seeded",
        class_="rules",
        entries=[("seed-entry", _entry("Seeded Entry"))],
    )

    assert source.tenant_id == LIBRARY_TENANT_ID
    assert source.current_version_id is not None

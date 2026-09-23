"""Superseded knowledge is not retrievable.

Publishing a new version does not remove the old version's chunks -- they keep their
embeddings and their tsvector, and a vector search that does not filter on version ranks
them against the current ones. The failure that produced this test looked like a model
error rather than a retrieval one: a workspace re-analysed a repository, the new overview
was correct, and the agents went on citing the old one ("Automatic summary unavailable;
file listing:") because that text was still the nearest neighbour for the question they
asked. Nothing in the transcript said the citation was a version old.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from core.knowledge.retrieval.dense import search_dense
from core.knowledge.retrieval.sparse import search_sparse
from core.knowledge.retrieval.tests.conftest import (
    attach_to_workspace,
    publish_next_version,
    seed_chunk,
    seed_tenant_and_source,
    unit_vector,
    versions_of,
)
from core.knowledge.retrieval.versions import effective_version_ids
from core.ports.scope import ScopeSet
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


async def test_superseded_chunk_is_not_retrievable(db_available: None) -> None:
    tenant_id, source_id = await seed_tenant_and_source("versions-superseded")
    stale = await seed_chunk(
        tenant_id,
        source_id,
        entry_key="repo-overview",
        body_text="Automatic summary unavailable; file listing follows.",
        class_="lore",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )
    await publish_next_version(tenant_id, source_id)
    current = await seed_chunk(
        tenant_id,
        source_id,
        entry_key="repo-overview",
        body_text="Automatic summary of the repository, generated and complete.",
        class_="lore",
        scope_key="workspace_public",
        embedding=unit_vector(1),
    )

    version_ids = await versions_of(tenant_id)
    common = {
        "tenant_id": tenant_id,
        "scope_keys": ScopeSet({"workspace_public"}),
        "class_": "lore",
        "version_ids": version_ids,
    }

    # The stale chunk is the *exact* vector match for this query and still must not
    # appear: being nearest is not a reason to be readable.
    dense = await search_dense(query_embedding=unit_vector(0), **common)
    assert {h.chunk_id for h in dense} == {current}
    assert stale not in {h.chunk_id for h in dense}

    sparse = await search_sparse(query_text="automatic summary", **common)
    assert {h.chunk_id for h in sparse} == {current}


async def test_effective_versions_follow_the_workspace_not_the_source(
    db_available: None,
) -> None:
    """A pin is the workspace saying "not yet" to a new version. Resolving from the
    source alone would hand it the new one anyway."""
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"versions-pin-{uuid.uuid4().hex[:8]}"
    )
    from core.knowledge.authoring import create_source

    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    pinned = await publish_next_version(tenant_id, source.id)
    await attach_to_workspace(tenant_id, workspace_id, source.id)
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text(
                "UPDATE workspace_knowledge_attachment SET version_pin = :v "
                "WHERE workspace_id = :w AND knowledge_source_id = :s"
            ),
            {"v": pinned, "w": workspace_id, "s": source.id},
        )
    newer = await publish_next_version(tenant_id, source.id)

    resolved = await effective_version_ids(tenant_id, workspace_id)
    assert resolved == {pinned}
    assert newer not in resolved


async def test_a_workspace_with_nothing_attached_resolves_to_nothing(
    db_available: None,
) -> None:
    tenant_id, _owner_id, workspace_id = await seed_dev_tenant(
        slug=f"versions-empty-{uuid.uuid4().hex[:8]}"
    )
    assert await effective_version_ids(tenant_id, workspace_id) == frozenset()


@pytest.mark.parametrize("search", [search_dense, search_sparse])
async def test_empty_version_set_raises_rather_than_meaning_every_version(
    db_available: None, search
) -> None:
    tenant_id, _source_id = await seed_tenant_and_source("versions-empty-arg")
    kwargs = {"query_embedding": unit_vector(0)} if search is search_dense else {"query_text": "x"}
    with pytest.raises(ValueError):
        await search(
            tenant_id=tenant_id,
            scope_keys=ScopeSet({"workspace_public"}),
            class_="lore",
            version_ids=frozenset(),
            **kwargs,
        )

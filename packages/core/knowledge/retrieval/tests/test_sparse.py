"""A1.4 acceptance criteria for sparse (lexical) retrieval, against a live Postgres."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from core.knowledge.retrieval.sparse import SPARSE_SEARCH_SQL, search_sparse
from core.knowledge.retrieval.tests.conftest import seed_chunk, seed_tenant_and_source
from core.ports.scope import ScopeSet
from core.tenancy.scope import tenant_scope


async def test_keyword_match_ranks_above_unrelated_text(db_available: None) -> None:
    tenant_id, source_id = await seed_tenant_and_source("sparse-match")
    target = await seed_chunk(
        tenant_id,
        source_id,
        entry_key="grappling",
        body_text="Grappling requires a strength check against the target's defense.",
        class_="rules",
        scope_key="workspace_public",
    )
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="unrelated",
        body_text="The weather in the village is usually mild and pleasant.",
        class_="rules",
        scope_key="workspace_public",
    )

    hits = await search_sparse(
        tenant_id=tenant_id,
        scope_keys=ScopeSet({"workspace_public"}),
        class_="rules",
        query_text="grappling strength check",
    )

    assert hits[0].chunk_id == target
    assert hits[0].rank == 1


async def test_out_of_scope_chunk_never_appears_despite_matching_keywords(
    db_available: None,
) -> None:
    tenant_id, source_id = await seed_tenant_and_source("sparse-scope")
    secret = await seed_chunk(
        tenant_id,
        source_id,
        entry_key="secret",
        body_text="The thieves guild secret grappling technique.",
        class_="rules",
        scope_key="faction_thieves",
    )
    public = await seed_chunk(
        tenant_id,
        source_id,
        entry_key="public",
        body_text="Grappling is a standard combat action.",
        class_="rules",
        scope_key="workspace_public",
    )

    hits = await search_sparse(
        tenant_id=tenant_id,
        scope_keys=ScopeSet({"workspace_public"}),
        class_="rules",
        query_text="grappling",
    )

    chunk_ids = {h.chunk_id for h in hits}
    assert secret not in chunk_ids
    assert public in chunk_ids


async def test_class_filter_excludes_other_classes(db_available: None) -> None:
    tenant_id, source_id = await seed_tenant_and_source("sparse-class")
    rules_chunk = await seed_chunk(
        tenant_id,
        source_id,
        entry_key="rule",
        body_text="Grappling rules text.",
        class_="rules",
        scope_key="workspace_public",
    )
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="lore",
        body_text="Grappling lore text.",
        class_="lore",
        scope_key="workspace_public",
    )

    hits = await search_sparse(
        tenant_id=tenant_id,
        scope_keys=ScopeSet({"workspace_public"}),
        class_="rules",
        query_text="grappling",
    )
    assert {h.chunk_id for h in hits} == {rules_chunk}


async def test_no_match_returns_empty(db_available: None) -> None:
    tenant_id, source_id = await seed_tenant_and_source("sparse-nomatch")
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="rule",
        body_text="Grappling rules text.",
        class_="rules",
        scope_key="workspace_public",
    )

    hits = await search_sparse(
        tenant_id=tenant_id,
        scope_keys=ScopeSet({"workspace_public"}),
        class_="rules",
        query_text="xyzzyunmatchedterm",
    )
    assert hits == []


async def test_empty_scope_keys_raises(db_available: None) -> None:
    tenant_id, _source_id = await seed_tenant_and_source("sparse-empty")
    with pytest.raises(ValueError):
        await search_sparse(
            tenant_id=tenant_id,
            scope_keys=ScopeSet(),
            class_="rules",
            query_text="grappling",
        )


async def test_scope_and_class_pushdown_is_in_the_query_plan(db_available: None) -> None:
    tenant_id, source_id = await seed_tenant_and_source("sparse-explain")
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="rule",
        body_text="Grappling rules text.",
        class_="rules",
        scope_key="workspace_public",
    )

    async with tenant_scope(tenant_id) as session:
        plan_rows = (
            await session.execute(
                text(f"EXPLAIN (FORMAT JSON) {SPARSE_SEARCH_SQL}"),
                {
                    "tenant_id": tenant_id,
                    "scope_keys": ["workspace_public"],
                    "class_": "rules",
                    "query_text": "grappling",
                    "k": 64,
                },
            )
        ).all()

    plan_text = json.dumps(plan_rows[0][0])
    assert "scope_key" in plan_text
    assert "class" in plan_text
    assert "tenant_id" in plan_text

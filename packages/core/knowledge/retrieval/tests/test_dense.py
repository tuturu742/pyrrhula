"""A1.4 acceptance criteria for dense (vector) retrieval, against a live Postgres."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from core.knowledge.retrieval.dense import DENSE_SEARCH_SQL, search_dense
from core.knowledge.retrieval.tests.conftest import (
    seed_chunk,
    seed_tenant_and_source,
    unit_vector,
    versions_of,
)
from core.ports.scope import ScopeSet
from core.tenancy.scope import tenant_scope


async def test_exact_match_ranks_first_and_scores_near_one(db_available: None) -> None:
    tenant_id, source_id = await seed_tenant_and_source("dense-exact")
    target = await seed_chunk(
        tenant_id,
        source_id,
        entry_key="grappling",
        body_text="Roll 1d20+STR to grapple.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="stealth",
        body_text="Roll 1d20+DEX to hide.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(1),
    )

    hits = await search_dense(
        tenant_id=tenant_id,
        scope_keys=ScopeSet({"workspace_public"}),
        class_="rules",
        version_ids=await versions_of(tenant_id),
        query_embedding=unit_vector(0),
    )

    assert hits[0].chunk_id == target
    assert hits[0].rank == 1
    assert hits[0].score == pytest.approx(1.0, abs=1e-6)


async def test_out_of_scope_chunk_never_appears_even_as_exact_match(
    db_available: None,
) -> None:
    """Adversarial case: the best-matching vector in the whole tenant belongs to a
    chunk outside the caller's requested scope -- it must never surface."""
    tenant_id, source_id = await seed_tenant_and_source("dense-scope")
    secret = await seed_chunk(
        tenant_id,
        source_id,
        entry_key="secret-plan",
        body_text="The thieves' guild secret plan.",
        class_="rules",
        scope_key="faction_thieves",
        embedding=unit_vector(0),
    )
    public = await seed_chunk(
        tenant_id,
        source_id,
        entry_key="public-rule",
        body_text="Public rule text.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(2),
    )

    hits = await search_dense(
        tenant_id=tenant_id,
        scope_keys=ScopeSet({"workspace_public"}),
        class_="rules",
        version_ids=await versions_of(tenant_id),
        query_embedding=unit_vector(0),  # identical to the out-of-scope chunk's vector
    )

    chunk_ids = {h.chunk_id for h in hits}
    assert secret not in chunk_ids
    assert public in chunk_ids


async def test_class_filter_excludes_other_classes(db_available: None) -> None:
    tenant_id, source_id = await seed_tenant_and_source("dense-class")
    rules_chunk = await seed_chunk(
        tenant_id,
        source_id,
        entry_key="rule",
        body_text="A rule.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="lore",
        body_text="Some lore.",
        class_="lore",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )

    hits = await search_dense(
        tenant_id=tenant_id,
        scope_keys=ScopeSet({"workspace_public"}),
        class_="rules",
        version_ids=await versions_of(tenant_id),
        query_embedding=unit_vector(0),
    )

    assert {h.chunk_id for h in hits} == {rules_chunk}


async def test_empty_scope_keys_raises(db_available: None) -> None:
    tenant_id, _source_id = await seed_tenant_and_source("dense-empty")
    with pytest.raises(ValueError):
        await search_dense(
            tenant_id=tenant_id,
            scope_keys=ScopeSet(),
            class_="rules",
            version_ids=await versions_of(tenant_id),
            query_embedding=unit_vector(0),
        )


async def test_second_tenant_cannot_see_first_tenants_chunks(db_available: None) -> None:
    tenant_a, source_a = await seed_tenant_and_source("dense-tenant-a")
    tenant_b, source_b = await seed_tenant_and_source("dense-tenant-b")
    chunk_a = await seed_chunk(
        tenant_a,
        source_a,
        entry_key="rule",
        body_text="Tenant A rule.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )
    await seed_chunk(
        tenant_b,
        source_b,
        entry_key="rule",
        body_text="Tenant B rule.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )

    hits = await search_dense(
        tenant_id=tenant_a,
        scope_keys=ScopeSet({"workspace_public"}),
        class_="rules",
        # Both tenants' versions, deliberately: a version id is not a secret, and naming
        # tenant B's must not make tenant B's chunk reachable.
        version_ids=await versions_of(tenant_a) | await versions_of(tenant_b),
        query_embedding=unit_vector(0),
    )
    assert {h.chunk_id for h in hits} == {chunk_a}


async def test_scope_and_class_pushdown_is_in_the_query_plan(db_available: None) -> None:
    """INV-4: proves the filter is a SQL predicate the planner actually uses, not a
    post-filter applied in Python -- by running the *exact* production query text
    (DENSE_SEARCH_SQL) through EXPLAIN and inspecting the plan itself."""
    tenant_id, source_id = await seed_tenant_and_source("dense-explain")
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="rule",
        body_text="A rule.",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )

    vector_literal = "[" + ",".join(["0.0"] * 1024) + "]"
    async with tenant_scope(tenant_id) as session:
        plan_rows = (
            await session.execute(
                text(f"EXPLAIN (FORMAT JSON) {DENSE_SEARCH_SQL}"),
                {
                    "tenant_id": tenant_id,
                    "scope_keys": ["workspace_public"],
                    "class_": "rules",
                    "version_ids": list(await versions_of(tenant_id)),
                    "qvec": vector_literal,
                    "k": 64,
                },
            )
        ).all()

    plan_text = json.dumps(plan_rows[0][0])
    assert "scope_key" in plan_text
    assert "class" in plan_text
    assert "tenant_id" in plan_text
    assert "version_id" in plan_text

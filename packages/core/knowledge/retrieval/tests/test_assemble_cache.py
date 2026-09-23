"""A1.9 acceptance criteria for the cache wired into the full pipeline: a cache hit must
be genuinely skipping the dense+sparse search, not merely returning an equal-by-chance
result -- proven by deleting the underlying chunks between calls and confirming the
second call still succeeds with the identical result. Also confirms scope_set and
version_set are real partitions of the cache, matching the isolation acceptance criteria
that live at the unit level in test_cache.py.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from sqlalchemy import text

from core.knowledge.retrieval.assemble import search_and_budget
from core.knowledge.retrieval.cache import RetrievalCache
from core.knowledge.retrieval.tests.conftest import (
    seed_chunk,
    seed_tenant_and_source,
    unit_vector,
    versions_of,
)
from core.ports.scope import ScopeSet
from core.tenancy.scope import tenant_scope


@pytest_asyncio.fixture
async def cache(redis_available: None) -> RetrievalCache:
    from api.redis_client import get_redis

    return RetrievalCache(get_redis(), ttl_seconds=5)


async def _seed_corpus(tenant_id, source_id) -> None:  # noqa: ANN001
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="rule-a",
        body_text="grapple check strength versus target defense value",
        class_="rules",
        scope_key="workspace_public",
        embedding=unit_vector(0),
    )


async def _delete_all_chunks(tenant_id) -> None:  # noqa: ANN001
    async with tenant_scope(tenant_id) as session:
        await session.execute(text("DELETE FROM knowledge_chunk"))
        await session.execute(text("DELETE FROM knowledge_entry"))


async def test_cache_hit_survives_the_underlying_chunks_being_deleted(
    db_available: None, cache: RetrievalCache
) -> None:
    tenant_id, source_id = await seed_tenant_and_source("assemble-cache-hit")
    await _seed_corpus(tenant_id, source_id)

    common_kwargs = {
        "tenant_id": tenant_id,
        "scope_keys": ScopeSet({"workspace_public"}),
        "query_embedding": unit_vector(0),
        "query_text": "grapple check strength",
        "class_ratios": {"rules": 1.0},
        "max_tokens": 100,
        "version_set": await versions_of(tenant_id),
        "cache": cache,
    }

    first = await search_and_budget(**common_kwargs)
    assert [c.entry_key for c in first] == ["rule-a"]
    assert cache.misses == 1
    assert cache.hits == 0

    await _delete_all_chunks(tenant_id)

    second = await search_and_budget(**common_kwargs)
    assert [c.entry_key for c in second] == ["rule-a"]
    assert cache.hits == 1


async def test_different_scope_keys_never_share_a_cached_result(
    db_available: None, cache: RetrievalCache
) -> None:
    tenant_id, source_id = await seed_tenant_and_source("assemble-cache-scope")
    await seed_chunk(
        tenant_id,
        source_id,
        entry_key="gm-secret",
        body_text="grapple check strength versus target defense value",
        class_="rules",
        scope_key="gm_only",
        embedding=unit_vector(0),
    )

    gm_result = await search_and_budget(
        tenant_id=tenant_id,
        scope_keys=ScopeSet({"workspace_public", "gm_only"}),
        query_embedding=unit_vector(0),
        query_text="grapple check strength",
        class_ratios={"rules": 1.0},
        max_tokens=100,
        version_set=await versions_of(tenant_id),
        cache=cache,
    )
    assert [c.entry_key for c in gm_result] == ["gm-secret"]

    player_result = await search_and_budget(
        tenant_id=tenant_id,
        scope_keys=ScopeSet({"workspace_public"}),
        query_embedding=unit_vector(0),
        query_text="grapple check strength",
        class_ratios={"rules": 1.0},
        max_tokens=100,
        version_set=await versions_of(tenant_id),
        cache=cache,
    )
    # Different scope_set -> different cache key -> a real (miss) search, which correctly
    # finds nothing, rather than an accidental hit leaking the gm_only chunk.
    assert player_result == []
    assert cache.misses == 2


async def test_different_version_sets_produce_independent_cache_entries(
    db_available: None, cache: RetrievalCache
) -> None:
    tenant_id, source_id = await seed_tenant_and_source("assemble-cache-version")
    await _seed_corpus(tenant_id, source_id)
    v1, v2 = uuid.uuid4(), uuid.uuid4()

    common_kwargs = {
        "tenant_id": tenant_id,
        "scope_keys": ScopeSet({"workspace_public"}),
        "query_embedding": unit_vector(0),
        "query_text": "grapple check strength",
        "class_ratios": {"rules": 1.0},
        "max_tokens": 100,
        "cache": cache,
    }

    await search_and_budget(version_set=frozenset({v1}), **common_kwargs)
    await search_and_budget(version_set=frozenset({v1}), **common_kwargs)
    await search_and_budget(version_set=frozenset({v2}), **common_kwargs)

    # First v1 call misses, second v1 call hits, v2 (different key) misses again.
    assert cache.misses == 2
    assert cache.hits == 1

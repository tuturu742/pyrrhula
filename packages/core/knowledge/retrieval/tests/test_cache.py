"""Acceptance criteria: the cache key includes scope_set and version_set, so two
principals with different scopes never share an entry, and publishing a new version
(which changes the resolved version_set) misses rather than returning stale candidates.
"""

from __future__ import annotations

import uuid

import pytest_asyncio

from core.knowledge.retrieval.cache import CacheKey, RetrievalCache, make_query_hash
from core.knowledge.retrieval.wrrf import FusedHit


def _hit(entry_key: str = "rule-a") -> FusedHit:
    return FusedHit(
        chunk_id=uuid.uuid4(),
        entry_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        entry_key=entry_key,
        token_count=10,
        rank=1,
        wrrf_score=0.5,
        contributing_lists=("dense",),
    )


def _key(
    *,
    query_text: str = "grapple check",
    scope: frozenset[str] = frozenset({"workspace_public"}),
    class_: str = "rules",
    version_set: frozenset[uuid.UUID | None] = frozenset(),
) -> CacheKey:
    return CacheKey(
        query_hash=make_query_hash(query_text),
        scope_set=scope,
        class_=class_,
        version_set=version_set,
    )


def test_digest_is_deterministic_regardless_of_set_construction_order() -> None:
    a = _key(scope=frozenset({"workspace_public", "gm_only"}))
    b = _key(scope=frozenset({"gm_only", "workspace_public"}))
    assert a.digest() == b.digest()


def test_different_scope_sets_never_share_a_digest() -> None:
    a = _key(scope=frozenset({"workspace_public"}))
    b = _key(scope=frozenset({"workspace_public", "gm_only"}))
    assert a.digest() != b.digest()


def test_different_version_sets_never_share_a_digest() -> None:
    v1, v2 = uuid.uuid4(), uuid.uuid4()
    a = _key(version_set=frozenset({v1}))
    b = _key(version_set=frozenset({v2}))
    assert a.digest() != b.digest()


def test_different_class_never_shares_a_digest() -> None:
    a = _key(class_="rules")
    b = _key(class_="lore")
    assert a.digest() != b.digest()


def test_different_query_text_never_shares_a_digest() -> None:
    a = _key(query_text="grapple check")
    b = _key(query_text="stealth check")
    assert a.digest() != b.digest()


@pytest_asyncio.fixture
async def cache(redis_available: None) -> RetrievalCache:
    from api.redis_client import get_redis

    return RetrievalCache(get_redis(), ttl_seconds=5)


async def test_set_then_get_round_trips_hits(cache: RetrievalCache) -> None:
    tenant_id = uuid.uuid4()
    key = _key()
    hit = _hit()

    assert await cache.get(tenant_id, key) is None
    await cache.set(tenant_id, key, [hit])
    cached = await cache.get(tenant_id, key)

    assert cached is not None
    assert cached[0].chunk_id == hit.chunk_id
    assert cached[0].entry_key == hit.entry_key
    assert cached[0].contributing_lists == hit.contributing_lists


async def test_two_tenants_with_the_same_key_do_not_share_an_entry(
    cache: RetrievalCache,
) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    key = _key()

    await cache.set(tenant_a, key, [_hit("tenant-a-hit")])

    assert await cache.get(tenant_a, key) is not None
    assert await cache.get(tenant_b, key) is None


async def test_different_scope_sets_never_return_each_others_cached_hits(
    cache: RetrievalCache,
) -> None:
    tenant_id = uuid.uuid4()
    principal_scoped_key = _key(scope=frozenset({"workspace_public"}))
    gm_scoped_key = _key(scope=frozenset({"workspace_public", "gm_only"}))

    await cache.set(tenant_id, gm_scoped_key, [_hit("gm-secret")])

    assert await cache.get(tenant_id, principal_scoped_key) is None
    assert await cache.get(tenant_id, gm_scoped_key) is not None


async def test_publishing_a_new_version_changes_the_key_and_misses(
    cache: RetrievalCache,
) -> None:
    tenant_id = uuid.uuid4()
    v1, v2 = uuid.uuid4(), uuid.uuid4()
    key_v1 = _key(version_set=frozenset({v1}))
    key_v2 = _key(version_set=frozenset({v2}))

    await cache.set(tenant_id, key_v1, [_hit("stale-from-v1")])

    assert await cache.get(tenant_id, key_v2) is None
    assert await cache.get(tenant_id, key_v1) is not None


async def test_invalidate_tenant_clears_all_entries_for_that_tenant_only(
    cache: RetrievalCache,
) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    key_rules = _key(class_="rules")
    key_lore = _key(class_="lore")

    await cache.set(tenant_a, key_rules, [_hit("a-rules")])
    await cache.set(tenant_a, key_lore, [_hit("a-lore")])
    await cache.set(tenant_b, key_rules, [_hit("b-rules")])

    cleared = await cache.invalidate_tenant(tenant_a)

    assert cleared == 2
    assert await cache.get(tenant_a, key_rules) is None
    assert await cache.get(tenant_a, key_lore) is None
    assert await cache.get(tenant_b, key_rules) is not None


async def test_hit_and_miss_counters_track_hit_rate(cache: RetrievalCache) -> None:
    tenant_id = uuid.uuid4()
    key = _key()

    await cache.get(tenant_id, key)  # miss
    await cache.set(tenant_id, key, [_hit()])
    await cache.get(tenant_id, key)  # hit
    await cache.get(tenant_id, key)  # hit

    assert cache.hits == 2
    assert cache.misses == 1
    assert cache.hit_rate == 2 / 3

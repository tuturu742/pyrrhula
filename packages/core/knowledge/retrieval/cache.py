"""Redis cache over the per-class retrieval cascade ( cost note, A1.9):
within a scene, the same content re-queries for many turns, so hit rates should be high —
cache the expensive part (post-WRRF fusion, pre-rerank candidates) and let reranking
(query-specific and cheap) run fresh every time.

Cache key is ``(query_hash, scope_set, class, version_set)`` — **the scope set is part of
the key**: a cache that ignores scope is a leak surface, the same INV-4 concern retrieval
itself has to take seriously. ``version_set`` (the resolved effective version id per
knowledge source relevant to this scope+class — the ``resolve_effective_version_id``)
makes publishing a new version self-invalidating: the key simply changes, so a stale
cached list is never reachable again, no explicit bust needed for that event specifically.

Attachment changes that *don't* publish a new version (enable/disable, scope_key change,
pin change to an *existing* version rather than the latest) don't change any version id,
so they wouldn't otherwise self-invalidate — ``invalidate_tenant`` covers those, at
tenant granularity (coarser than "just this scope/class", but correct: over-invalidating
a cache is a performance cost, under-invalidating is a leak-adjacent correctness bug, and
this is a v1 tradeoff made deliberately, not a hidden gap).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis

from core.knowledge.retrieval.wrrf import FusedHit
from core.observability.otel import get_tracer
from core.ports.scope import ScopeSet

_tracer = get_tracer(__name__)

_KEY_PREFIX = "retrieval_cache"
_TENANT_INDEX_PREFIX = "retrieval_cache_keys"
_DEFAULT_TTL_SECONDS = 300


def make_query_hash(query_text: str) -> str:
    return hashlib.sha256(query_text.encode()).hexdigest()


@dataclass(frozen=True)
class CacheKey:
    query_hash: str
    scope_set: ScopeSet
    class_: str
    version_set: frozenset[uuid.UUID | None]

    def digest(self) -> str:
        """Deterministic regardless of set iteration order — same logical key always
        produces the same string, which is what makes "same inputs, same cache slot" true."""
        parts = [
            self.query_hash,
            ",".join(sorted(self.scope_set)),
            self.class_,
            ",".join(sorted(str(v) for v in self.version_set)),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _redis_key(tenant_id: uuid.UUID, key: CacheKey) -> str:
    return f"{_KEY_PREFIX}:{tenant_id}:{key.digest()}"


def _tenant_index_key(tenant_id: uuid.UUID) -> str:
    return f"{_TENANT_INDEX_PREFIX}:{tenant_id}"


def _serialize(hits: list[FusedHit]) -> str:
    return json.dumps(
        [
            {
                "chunk_id": str(h.chunk_id),
                "entry_id": str(h.entry_id),
                "source_id": str(h.source_id),
                "version_id": str(h.version_id) if h.version_id else None,
                "entry_key": h.entry_key,
                "token_count": h.token_count,
                "rank": h.rank,
                "wrrf_score": h.wrrf_score,
                "contributing_lists": list(h.contributing_lists),
            }
            for h in hits
        ]
    )


def _deserialize(raw: str) -> list[FusedHit]:
    return [
        FusedHit(
            chunk_id=uuid.UUID(row["chunk_id"]),
            entry_id=uuid.UUID(row["entry_id"]),
            source_id=uuid.UUID(row["source_id"]),
            version_id=uuid.UUID(row["version_id"]) if row["version_id"] else None,
            entry_key=row["entry_key"],
            token_count=row["token_count"],
            rank=row["rank"],
            wrrf_score=row["wrrf_score"],
            contributing_lists=tuple(row["contributing_lists"]),
        )
        for row in json.loads(raw)
    ]


class RetrievalCache:
    def __init__(self, redis: Redis, *, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self.hits = 0
        self.misses = 0

    async def get(self, tenant_id: uuid.UUID, key: CacheKey) -> list[FusedHit] | None:
        with _tracer.start_as_current_span("retrieval_cache.get") as span:
            raw = await self._redis.get(_redis_key(tenant_id, key))
            hit = raw is not None
            span.set_attribute("pyrrhula.retrieval_cache.hit", hit)
            if hit:
                self.hits += 1
                assert isinstance(raw, str)  # this cache requires a decode_responses=True client
                return _deserialize(raw)
            self.misses += 1
            return None

    async def set(self, tenant_id: uuid.UUID, key: CacheKey, hits: list[FusedHit]) -> None:
        redis_key = _redis_key(tenant_id, key)
        await self._redis.set(redis_key, _serialize(hits), ex=self._ttl_seconds)
        index_key = _tenant_index_key(tenant_id)
        await self._redis.sadd(index_key, redis_key)
        await self._redis.expire(index_key, self._ttl_seconds)

    async def invalidate_tenant(self, tenant_id: uuid.UUID) -> int:
        """Attachment changes that don't publish a new version (enable/disable, scope_key
        change, re-pinning to an already-existing version) don't change any version id, so
        the cache key wouldn't otherwise change -- call this to force a clean slate.
        Tenant-wide, not scoped to the specific attachment that changed: simpler and
        still correct, at the cost of also dropping unrelated cache entries."""
        index_key = _tenant_index_key(tenant_id)
        members = await self._redis.smembers(index_key)
        if members:
            await self._redis.delete(*members)
        await self._redis.delete(index_key)
        return len(members)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

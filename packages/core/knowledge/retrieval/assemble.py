"""Search + fuse + (optionally rerank) + budget, tied together (-6,
A1.6/A1.7) — the slice of the future ``ContextAssembler`` that makes rule-vs-lore
priority real: per-class dense+sparse+keyed retrieval, WRRF fusion, class-blind rerank,
then bucketed budget fill. Deliberately *not* the full assembler: no
``VisibilityResolver``, no entity-state rendering, no secrets gate, no prompt
layout, no ``ContextManifest`` persistence  — those are later tasks' jobs. This
function's whole purpose is the one property the plan calls the core design bet: change a
budget ratio, get a deterministically different, fully explainable included set, with a
rules entry and a lore entry never once competing for the same slot.

``reranker=None`` (default) skips reranking entirely — ranking falls back to WRRF order,
unbounded by the reranker's top-32-in/16-out cap (the "config to disable reranking,
degraded mode for tiny deployments").

``cache`` memoizes the *fully fused* per-class candidate list — including the
keyed/activation contribution, not just dense+sparse — keyed by ``(query_hash, scope_set,
class, version_set)``. That means a cache hit can reflect a slightly stale activation
state (sticky/cooldown windows) for up to the cache's TTL, a deliberate, bounded
trade-off: within a scene, activation state is itself usually stable turn-to-turn, and the
whole point of a short TTL is trading a little staleness for not re-running the expensive
dense+sparse cascade on every turn.

``version_set`` is required and does two jobs: it is the SQL predicate dense and sparse
filter on, and it is part of the cache key. Publishing a new version changes it, which
changes both — so a stale read is structurally impossible rather than merely unlikely.
Resolve it with ``versions.effective_version_ids(tenant_id, workspace_id)``. An empty set
means the workspace has nothing attached: there is nothing to retrieve, and this returns
nothing rather than everything.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from core.knowledge.activation import ActivatedEntry
from core.knowledge.retrieval.budget import (
    DEFAULT_SPILL,
    BudgetedChunk,
    apply_priority_weight_override,
    fill_all_buckets,
    split_budget,
    to_budgeted_chunks,
)
from core.knowledge.retrieval.cache import CacheKey, RetrievalCache, make_query_hash
from core.knowledge.retrieval.dense import RetrievalHit, search_dense
from core.knowledge.retrieval.keyed import expand_activated_entries_to_chunks
from core.knowledge.retrieval.rerank import fetch_chunk_texts, rerank_bucket
from core.knowledge.retrieval.sparse import search_sparse
from core.knowledge.retrieval.wrrf import DEFAULT_LIST_WEIGHTS, DEFAULT_WRRF_K, fuse
from core.ports.reranker import Reranker
from core.ports.scope import ScopeSet

_DEFAULT_K_PER_LIST = 64


async def search_and_budget(
    tenant_id: uuid.UUID,
    *,
    scope_keys: ScopeSet,
    query_embedding: Sequence[float],
    query_text: str,
    class_ratios: dict[str, float],
    max_tokens: int,
    version_set: frozenset[uuid.UUID],
    activated_entries_by_class: dict[str, list[ActivatedEntry]] | None = None,
    priority_weights: dict[str, float] | None = None,
    spill: str = DEFAULT_SPILL,
    k_per_list: int = _DEFAULT_K_PER_LIST,
    wrrf_k: int = DEFAULT_WRRF_K,
    list_weights: dict[str, float] | None = None,
    reranker: Reranker | None = None,
    cache: RetrievalCache | None = None,
) -> list[BudgetedChunk]:
    ratios = class_ratios
    if priority_weights:
        ratios = apply_priority_weight_override(ratios, priority_weights)
    bucket_tokens = split_budget(ratios, max_tokens)

    ranked_by_class = {}
    constant_by_class = {}

    for class_ in ratios:
        activated = (activated_entries_by_class or {}).get(class_, [])
        keyed_hits = await expand_activated_entries_to_chunks(tenant_id, activated)
        constant_entry_ids = {a.entry_id for a in activated if a.why == "constant"}
        constant_by_class[class_] = frozenset(
            h.chunk_id for h in keyed_hits if h.entry_id in constant_entry_ids
        )

        cache_key = None
        fused = None
        if cache is not None:
            cache_key = CacheKey(
                query_hash=make_query_hash(query_text),
                scope_set=scope_keys,
                class_=class_,
                version_set=frozenset(version_set),
            )
            fused = await cache.get(tenant_id, cache_key)

        if fused is None:
            # Nothing attached means nothing published to search. The keyed list is built
            # from the same attachments, so it is empty too; fusing the three empty lists
            # keeps one code path rather than two.
            dense_hits: list[RetrievalHit] = []
            sparse_hits: list[RetrievalHit] = []
            if version_set:
                dense_hits = await search_dense(
                    tenant_id=tenant_id,
                    scope_keys=scope_keys,
                    class_=class_,
                    version_ids=version_set,
                    query_embedding=query_embedding,
                    k=k_per_list,
                )
                sparse_hits = await search_sparse(
                    tenant_id=tenant_id,
                    scope_keys=scope_keys,
                    class_=class_,
                    version_ids=version_set,
                    query_text=query_text,
                    k=k_per_list,
                )
            fused = fuse(
                {"dense": dense_hits, "sparse": sparse_hits, "keyed": keyed_hits},
                k=wrrf_k,
                list_weights=list_weights if list_weights is not None else DEFAULT_LIST_WEIGHTS,
            )
            if cache is not None and cache_key is not None:
                await cache.set(tenant_id, cache_key, fused)

        if reranker is not None:
            chunk_texts = await fetch_chunk_texts(tenant_id, [h.chunk_id for h in fused])
            fused = await rerank_bucket(query_text, fused, reranker, chunk_texts)

        ranked_by_class[class_] = fused

    fill_results = fill_all_buckets(
        ranked_by_class,
        bucket_tokens,
        constant_chunk_ids_by_class=constant_by_class,
        spill=spill,
    )
    return to_budgeted_chunks(fill_results, constant_chunk_ids_by_class=constant_by_class)

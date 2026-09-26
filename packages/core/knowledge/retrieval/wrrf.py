"""Weighted Reciprocal Rank Fusion: fuses per-class
dense/sparse/keyed candidate lists on **ranks**, never scores. This is the entire point of
D2 — sparse (BM25-ish) and dense (cosine) scores live in different, incommensurable
distributions, and multiplying either by a class-priority weight produces a number with no
interpretation. RRF sidesteps the normalisation problem by never looking at a raw score at
all, only at each candidate's rank position within its own originating list.

Fuse-before-rerank (this module runs before the reranker, not after) is the
empirically better order per the plan's citation of TREC iKAT 2025 and the standard
two-stage cascade pattern.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from core.knowledge.retrieval.dense import RetrievalHit

DEFAULT_WRRF_K = 60
DEFAULT_LIST_WEIGHTS: dict[str, float] = {"dense": 1.0, "sparse": 0.8, "keyed": 1.2}


@dataclass(frozen=True)
class FusedHit:
    chunk_id: uuid.UUID
    entry_id: uuid.UUID
    source_id: uuid.UUID
    version_id: uuid.UUID | None
    entry_key: str
    token_count: int
    rank: int
    wrrf_score: float
    contributing_lists: tuple[str, ...]


def fuse(
    candidate_lists: dict[str, list[RetrievalHit]],
    *,
    k: int = DEFAULT_WRRF_K,
    list_weights: dict[str, float] | None = None,
) -> list[FusedHit]:
    """Same inputs always produce the same output (a property this task's acceptance
    criteria requires): no randomness anywhere, and ties are broken on ``chunk_id`` string
    order so the final ordering never depends on dict/set iteration order."""
    weights = list_weights if list_weights is not None else DEFAULT_LIST_WEIGHTS

    scores: dict[uuid.UUID, float] = {}
    contributing: dict[uuid.UUID, list[str]] = {}
    meta: dict[uuid.UUID, RetrievalHit] = {}

    for list_name, hits in candidate_lists.items():
        weight = weights.get(list_name, 1.0)
        for hit in hits:
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + weight / (k + hit.rank)
            contributing.setdefault(hit.chunk_id, []).append(list_name)
            meta.setdefault(hit.chunk_id, hit)

    ordered = sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))

    return [
        FusedHit(
            chunk_id=chunk_id,
            entry_id=meta[chunk_id].entry_id,
            source_id=meta[chunk_id].source_id,
            version_id=meta[chunk_id].version_id,
            entry_key=meta[chunk_id].entry_key,
            token_count=meta[chunk_id].token_count,
            rank=i + 1,
            wrrf_score=score,
            contributing_lists=tuple(contributing[chunk_id]),
        )
        for i, (chunk_id, score) in enumerate(ordered)
    ]

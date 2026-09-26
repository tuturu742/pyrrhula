"""Deterministic stub Reranker: word-overlap scoring, no model weights, no
network — keeps CI fast, matching the stub-embedding rationale exactly. Unlike a
pure hash-based stub, this one is meaningfully *query-sensitive* (a candidate sharing more
words with the query scores higher), so tests can verify the reranker actually reordered
candidates in a predictable direction without needing a real cross-encoder model.
"""

from __future__ import annotations

from collections.abc import Sequence

from core.ports.reranker import RerankCandidate, RerankResult


class StubReranker:
    model_name = "local/stub-reranker"

    async def rerank(self, query: str, candidates: Sequence[RerankCandidate]) -> list[RerankResult]:
        query_words = set(query.lower().split())
        results = []
        for candidate in candidates:
            candidate_words = set(candidate.text.lower().split())
            overlap = len(query_words & candidate_words)
            results.append(RerankResult(chunk_id=candidate.chunk_id, score=float(overlap)))
        return results

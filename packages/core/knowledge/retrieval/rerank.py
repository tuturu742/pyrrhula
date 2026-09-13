"""Reranking within a fused, budget-bucketed candidate list (plan §6.3 step 5, A1.7):
class-blind cross-encoder rerank of the top 32 WRRF-fused (A1.6) candidates, keeping the
top 16 by rerank score. Runs *between* WRRF fusion and bucket fill — fuse-before-rerank is
the empirically better cascade (plan cites TREC iKAT 2025 and the standard two-stage
pattern); do not "optimise" this order.

Reranking is optional at the call site (``core.knowledge.retrieval.assemble.
search_and_budget`` takes ``reranker: Reranker | None``) — a tiny deployment can disable
it entirely, in which case ranking falls back to WRRF order untouched.
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import text

from core.knowledge.retrieval.wrrf import FusedHit
from core.observability.otel import get_tracer
from core.ports.reranker import RerankCandidate, Reranker
from core.tenancy.scope import tenant_scope

_tracer = get_tracer(__name__)

TOP_K_IN = 32
TOP_K_OUT = 16


async def fetch_chunk_texts(
    tenant_id: uuid.UUID, chunk_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not chunk_ids:
        return {}
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, text FROM knowledge_chunk "
                    "WHERE tenant_id = :tenant_id AND id = ANY(:chunk_ids)"
                ),
                {"tenant_id": tenant_id, "chunk_ids": chunk_ids},
            )
        ).all()
    return {row[0]: row[1] for row in rows}


async def rerank_bucket(
    query_text: str,
    fused: list[FusedHit],
    reranker: Reranker,
    chunk_texts: dict[uuid.UUID, str],
    *,
    top_k_in: int = TOP_K_IN,
    top_k_out: int = TOP_K_OUT,
) -> list[FusedHit]:
    """``chunk_texts`` maps chunk_id -> chunk text (``FusedHit`` doesn't carry it — the
    same separation ``core.knowledge.embedding`` uses, accepting text as a parameter
    rather than fetching it itself; ``fetch_chunk_texts`` above is the DB-touching half a
    caller uses to build this dict)."""
    candidates_in = fused[:top_k_in]
    if not candidates_in:
        return []

    with _tracer.start_as_current_span("rerank.rerank_bucket") as span:
        span.set_attribute("pyrrhula.rerank.candidate_count", len(candidates_in))
        start = time.monotonic()

        rerank_candidates = [
            RerankCandidate(chunk_id=hit.chunk_id, text=chunk_texts.get(hit.chunk_id, ""))
            for hit in candidates_in
        ]
        results = await reranker.rerank(query_text, rerank_candidates)

        span.set_attribute("pyrrhula.rerank.latency_ms", (time.monotonic() - start) * 1000)

    score_by_chunk = {r.chunk_id: r.score for r in results}

    ordered = sorted(
        candidates_in,
        key=lambda hit: (-score_by_chunk.get(hit.chunk_id, float("-inf")), str(hit.chunk_id)),
    )[:top_k_out]

    return [
        FusedHit(
            chunk_id=hit.chunk_id,
            entry_id=hit.entry_id,
            source_id=hit.source_id,
            version_id=hit.version_id,
            entry_key=hit.entry_key,
            token_count=hit.token_count,
            rank=i + 1,
            wrrf_score=score_by_chunk.get(hit.chunk_id, hit.wrrf_score),
            contributing_lists=(*hit.contributing_lists, "reranked"),
        )
        for i, hit in enumerate(ordered)
    ]

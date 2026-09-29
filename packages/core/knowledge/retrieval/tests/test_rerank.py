"""Acceptance criteria for the rerank step, against a live Postgres."""

from __future__ import annotations

import uuid

from adapters.reranker.stub.provider import StubReranker
from core.knowledge.retrieval.rerank import TOP_K_IN, TOP_K_OUT, fetch_chunk_texts, rerank_bucket
from core.knowledge.retrieval.tests.conftest import seed_chunk, seed_tenant_and_source
from core.knowledge.retrieval.wrrf import FusedHit


def _fused_hit(chunk_id: uuid.UUID, rank: int, entry_key: str = "e") -> FusedHit:
    return FusedHit(
        chunk_id=chunk_id,
        entry_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        version_id=None,
        entry_key=entry_key,
        token_count=10,
        rank=rank,
        wrrf_score=1.0 / rank,
        contributing_lists=("dense",),
    )


async def test_fetch_chunk_texts_returns_real_chunk_text(db_available: None) -> None:
    tenant_id, source_id = await seed_tenant_and_source("rerank-fetch")
    chunk_id = await seed_chunk(
        tenant_id,
        source_id,
        entry_key="grappling",
        body_text="Roll 1d20+STR to grapple.",
        class_="rules",
        scope_key="workspace_public",
    )
    texts = await fetch_chunk_texts(tenant_id, [chunk_id])
    assert texts[chunk_id] == "Roll 1d20+STR to grapple."


async def test_fetch_chunk_texts_empty_input_returns_empty() -> None:
    assert await fetch_chunk_texts(uuid.uuid4(), []) == {}


async def test_rerank_reorders_by_query_relevance() -> None:
    """WRRF put `off_topic` first and `on_topic` second; the reranker (word overlap
    against the query) must flip that -- proving reranking actually changes order, not
    just re-labels it."""
    on_topic_id = uuid.uuid4()
    off_topic_id = uuid.uuid4()
    fused = [_fused_hit(off_topic_id, rank=1), _fused_hit(on_topic_id, rank=2)]
    chunk_texts = {
        off_topic_id: "the weather is mild and pleasant today",
        on_topic_id: "grapple check strength roll versus target defense",
    }

    reranked = await rerank_bucket("grapple check strength", fused, StubReranker(), chunk_texts)

    assert [h.chunk_id for h in reranked] == [on_topic_id, off_topic_id]
    assert reranked[0].rank == 1
    assert "reranked" in reranked[0].contributing_lists


async def test_rerank_caps_to_top_k_out_from_top_k_in() -> None:
    fused = [_fused_hit(uuid.uuid4(), rank=i + 1) for i in range(TOP_K_IN + 10)]
    chunk_texts = {h.chunk_id: "some text" for h in fused}

    reranked = await rerank_bucket("query", fused, StubReranker(), chunk_texts)

    assert len(reranked) == TOP_K_OUT
    # Candidates beyond top_k_in were never even considered for the output.
    considered_ids = {h.chunk_id for h in fused[:TOP_K_IN]}
    assert all(h.chunk_id in considered_ids for h in reranked)


async def test_rerank_empty_fused_list_returns_empty() -> None:
    assert await rerank_bucket("query", [], StubReranker(), {}) == []


async def test_rerank_preserves_entry_and_source_metadata() -> None:
    chunk_id = uuid.uuid4()
    entry_id = uuid.uuid4()
    source_id = uuid.uuid4()
    fused = [
        FusedHit(
            chunk_id=chunk_id,
            entry_id=entry_id,
            source_id=source_id,
            version_id=None,
            entry_key="grappling",
            token_count=42,
            rank=1,
            wrrf_score=0.5,
            contributing_lists=("dense", "sparse"),
        )
    ]
    reranked = await rerank_bucket("q", fused, StubReranker(), {chunk_id: "q"})
    assert reranked[0].entry_id == entry_id
    assert reranked[0].source_id == source_id
    assert reranked[0].entry_key == "grappling"
    assert reranked[0].token_count == 42
    assert reranked[0].contributing_lists == ("dense", "sparse", "reranked")


async def test_constant_entries_survive_the_rerank_caps_and_come_first() -> None:
    """An always-on entry is the author's call, not the reranker's. Fused last, scoring
    nothing against the query, and beyond top_k_in: it is still returned, and first."""
    constant_id = uuid.uuid4()
    fused = [_fused_hit(uuid.uuid4(), rank=i + 1) for i in range(TOP_K_IN + 5)]
    fused.append(_fused_hit(constant_id, rank=len(fused) + 1, entry_key="calling_for_a_roll"))
    chunk_texts = {h.chunk_id: "grapple check strength" for h in fused}
    chunk_texts[constant_id] = "nothing in common with the query"

    reranked = await rerank_bucket(
        "grapple check strength",
        fused,
        StubReranker(),
        chunk_texts,
        constant_chunk_ids=frozenset({constant_id}),
    )

    assert reranked[0].chunk_id == constant_id
    assert len(reranked) == TOP_K_OUT + 1

import uuid

from adapters.reranker.stub.provider import StubReranker
from core.ports.reranker import RerankCandidate


async def test_more_word_overlap_scores_higher() -> None:
    reranker = StubReranker()
    a = RerankCandidate(chunk_id=uuid.uuid4(), text="roll a d20 to grapple the orc")
    b = RerankCandidate(chunk_id=uuid.uuid4(), text="the weather is mild today")

    results = await reranker.rerank("grapple the orc", [a, b])
    scores = {r.chunk_id: r.score for r in results}
    assert scores[a.chunk_id] > scores[b.chunk_id]


async def test_no_overlap_scores_zero() -> None:
    reranker = StubReranker()
    candidate = RerankCandidate(chunk_id=uuid.uuid4(), text="completely unrelated content")
    results = await reranker.rerank("grappling rules", [candidate])
    assert results[0].score == 0.0


async def test_empty_candidates_returns_empty() -> None:
    reranker = StubReranker()
    assert await reranker.rerank("anything", []) == []

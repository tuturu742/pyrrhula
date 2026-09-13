"""Adapter wiring tests -- ``_load()`` is monkeypatched to a fake model so these never
need the real ~600MB bge-reranker-v2-m3 weights or a network call.
"""

from __future__ import annotations

import uuid

from adapters.reranker.local.provider import CrossEncoderReranker
from core.ports.reranker import RerankCandidate


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[list[tuple[str, str]]] = []

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        self.calls.append(pairs)
        return [float(len(text)) for _query, text in pairs]


async def test_rerank_calls_model_predict_with_query_text_pairs() -> None:
    reranker = CrossEncoderReranker("fake/model")
    fake_model = _FakeModel()
    reranker._model = fake_model  # bypass the real (lazy) HF download

    a = RerankCandidate(chunk_id=uuid.uuid4(), text="short")
    b = RerankCandidate(chunk_id=uuid.uuid4(), text="a much longer candidate text")

    results = await reranker.rerank("query", [a, b])

    assert fake_model.calls == [[("query", "short"), ("query", "a much longer candidate text")]]
    scores = {r.chunk_id: r.score for r in results}
    assert scores[b.chunk_id] > scores[a.chunk_id]


async def test_model_name_uses_local_prefix() -> None:
    reranker = CrossEncoderReranker("BAAI/bge-reranker-v2-m3")
    assert reranker.model_name == "local/BAAI/bge-reranker-v2-m3"


async def test_empty_candidates_never_loads_the_model() -> None:
    reranker = CrossEncoderReranker("fake/model")
    assert await reranker.rerank("query", []) == []
    assert reranker._model is None

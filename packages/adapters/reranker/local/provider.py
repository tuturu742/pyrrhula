"""v1 in-process Reranker: bge-reranker-v2-m3 via sentence-transformers' ``CrossEncoder``,
CPU by default. Lazy-loaded (first ``.rerank()`` call) — same reason as
``adapters.embedding.local.SentenceTransformersEmbeddingProvider``: constructing/testing
this adapter never requires the model weights already be present.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from core.ports.reranker import RerankCandidate, RerankResult


class CrossEncoderReranker:
    def __init__(self, model: str = "BAAI/bge-reranker-v2-m3") -> None:
        self._model_name = f"local/{model}"
        self._hf_model_name = model
        self._model: Any | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(self._hf_model_name)
        return self._model

    async def rerank(self, query: str, candidates: Sequence[RerankCandidate]) -> list[RerankResult]:
        if not candidates:
            return []
        model = self._load()
        pairs = [(query, c.text) for c in candidates]
        scores = await asyncio.to_thread(model.predict, pairs)
        return [
            RerankResult(chunk_id=c.chunk_id, score=float(s))
            for c, s in zip(candidates, scores, strict=True)
        ]

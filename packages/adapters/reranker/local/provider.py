"""v1 in-process Reranker: bge-reranker-v2-m3 via sentence-transformers' ``CrossEncoder``,
CPU by default. Lazy-loaded (first ``.rerank()`` call) — same reason as
``adapters.embedding.local.SentenceTransformersEmbeddingProvider``: constructing/testing
this adapter never requires the model weights already be present.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from typing import Any

from core.ports.reranker import RerankCandidate, RerankResult


class CrossEncoderReranker:
    def __init__(self, model: str = "BAAI/bge-reranker-v2-m3") -> None:
        self._model_name = f"local/{model}"
        self._hf_model_name = model
        self._model: Any | None = None
        self._load_lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._model_name

    def _load(self) -> Any:
        with self._load_lock:
            if self._model is None:
                from sentence_transformers import CrossEncoder

                # Same two points as the embedding provider: the files are local, so the
                # hub is never asked, and the load runs off the event loop.
                self._model = CrossEncoder(self._hf_model_name, local_files_only=True)
            return self._model

    async def rerank(self, query: str, candidates: Sequence[RerankCandidate]) -> list[RerankResult]:
        if not candidates:
            return []
        model = await asyncio.to_thread(self._load)
        pairs = [(query, c.text) for c in candidates]
        scores = await asyncio.to_thread(model.predict, pairs)
        return [
            RerankResult(chunk_id=c.chunk_id, score=float(s))
            for c, s in zip(candidates, scores, strict=True)
        ]

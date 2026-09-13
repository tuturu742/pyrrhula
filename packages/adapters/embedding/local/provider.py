"""v1 self-hosted EmbeddingProvider: bge-m3 (1024-dim) via sentence-transformers, CPU by
default (torch picks up a GPU automatically if one is visible — "GPU optional, CPU
workable", plan §13.9). ``sentence_transformers``/``torch`` are imported lazily, inside
``_load()``, not at module import time — importing this module (or constructing the
adapter) never requires the ~2GB of model weights to already be downloaded/cached; only
the first real ``.embed()`` call does. Mirrors ``LiteLLMModelProvider``'s lazy
``import litellm``/``import tiktoken`` pattern.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.ports.embedding import EmbedRequest, check_egress


class SentenceTransformersEmbeddingProvider:
    def __init__(self, model: str = "BAAI/bge-m3", *, dimension: int = 1024) -> None:
        self._model_name = f"local/{model}"
        self._hf_model_name = model
        self._dimension = dimension
        self._model: Any | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._hf_model_name)
        return self._model

    async def embed(self, req: EmbedRequest) -> list[list[float]]:
        check_egress(req.purpose, req.model, req.egress_policy)
        model = self._load()
        vectors = await asyncio.to_thread(model.encode, list(req.texts), normalize_embeddings=True)
        return [vector.tolist() for vector in vectors]

"""Deterministic stub EmbeddingProvider: fast, no model weights, no network — keeps
CI/tests fast. Same text always maps to the same vector (hash-based), so cache/reuse/
re-embed tests are meaningful without needing a real model.

Default ``dimension=8`` is only safe for the adapter's own isolated unit tests, which
never touch the real ``knowledge_chunk`` table — that column is a fixed ``vector(1024)``,
so any test that writes through it must construct this with
``dimension=1024`` (or, in the worker composition root, let the deployment's retrieval-model
setting drive it — see ``worker.embedding_provider_factory``).
"""

from __future__ import annotations

import hashlib

from core.ports.embedding import EmbedRequest, check_egress


class StubEmbeddingProvider:
    def __init__(self, *, model_name: str = "local/stub", dimension: int = 8) -> None:
        self._model_name = model_name
        self._dimension = dimension

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed(self, req: EmbedRequest) -> list[list[float]]:
        check_egress(req.purpose, req.model, req.egress_policy)
        return [self._embed_one(text) for text in req.texts]

    def _embed_one(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        return [(digest[i % len(digest)] / 127.5) - 1.0 for i in range(self._dimension)]

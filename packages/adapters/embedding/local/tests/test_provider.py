"""Adapter wiring tests -- ``_load()`` is monkeypatched to a fake model so these never
need the real ~2GB bge-m3 weights or a network call; they prove the adapter's own logic
(batching through ``model.encode``, normalising numpy-ish output to plain lists,
egress-before-load) is correct.
"""

from __future__ import annotations

import pytest

from adapters.embedding.local.provider import SentenceTransformersEmbeddingProvider
from core.ports.embedding import EmbedRequest
from core.ports.model_provider import EgressDeniedError


class _FakeVector(list):
    def tolist(self) -> list[float]:
        return list(self)


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []

    def encode(self, texts: list[str], normalize_embeddings: bool = False) -> list[_FakeVector]:
        self.calls.append((texts, normalize_embeddings))
        return [_FakeVector([float(len(t))] * 4) for t in texts]


async def test_embed_calls_model_encode_and_normalises_output() -> None:
    provider = SentenceTransformersEmbeddingProvider("fake/model", dimension=4)
    fake_model = _FakeModel()
    provider._model = fake_model  # bypass the real (lazy) HF download

    req = EmbedRequest(model=provider.model_name, texts=["hi", "hello there"])
    vectors = await provider.embed(req)

    assert fake_model.calls == [(["hi", "hello there"], True)]
    assert vectors == [[2.0, 2.0, 2.0, 2.0], [11.0, 11.0, 11.0, 11.0]]


async def test_model_name_uses_local_prefix() -> None:
    provider = SentenceTransformersEmbeddingProvider("BAAI/bge-m3", dimension=1024)
    assert provider.model_name == "local/BAAI/bge-m3"
    assert provider.dimension == 1024


async def test_egress_denied_before_loading_the_model() -> None:
    provider = SentenceTransformersEmbeddingProvider("fake/model", dimension=4)
    req = EmbedRequest(model=provider.model_name, texts=["hi"], egress_policy={"embed": ["cloud"]})
    with pytest.raises(EgressDeniedError):
        await provider.embed(req)
    assert provider._model is None  # never touched _load()

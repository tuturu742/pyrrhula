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
    def __init__(self, max_seq_length: int = 8192) -> None:
        self.calls: list[tuple[list[str], bool]] = []
        self.batch_sizes: list[int] = []
        self.max_seq_length = max_seq_length

    def encode(
        self,
        texts: list[str],
        normalize_embeddings: bool = False,
        batch_size: int = 32,
    ) -> list[_FakeVector]:
        self.calls.append((texts, normalize_embeddings))
        self.batch_sizes.append(batch_size)
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


async def test_encode_is_given_a_bounded_batch_size() -> None:
    """Regression: `.encode()` was called without `batch_size`, so sentence-transformers
    used its own default (32) and peak memory sized itself off the caller's data. A real
    source-code corpus at that batch size OOM-killed the worker (exit 137, 8Gi limit)."""
    provider = SentenceTransformersEmbeddingProvider("fake/model", dimension=4, batch_size=8)
    fake_model = _FakeModel()
    provider._model = fake_model

    await provider.embed(EmbedRequest(model=provider.model_name, texts=["a", "b"]))

    assert fake_model.batch_sizes == [8]


def test_load_caps_the_models_sequence_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """bge-m3 ships max_seq_length=8192; attention is quadratic in it, so the cap is the
    other half of the OOM fix."""
    fake_model = _FakeModel(max_seq_length=8192)
    provider = SentenceTransformersEmbeddingProvider("fake/model", dimension=4, max_seq_length=1024)
    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer", lambda name: fake_model, raising=False
    )

    assert provider._load().max_seq_length == 1024


def test_load_never_raises_a_models_own_shorter_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A checkpoint that only supports 512 must stay at 512 -- the cap is a ceiling, not an
    assignment, or it would silently ask a model for sequences it cannot encode."""
    fake_model = _FakeModel(max_seq_length=512)
    provider = SentenceTransformersEmbeddingProvider("fake/model", dimension=4, max_seq_length=1024)
    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer", lambda name: fake_model, raising=False
    )

    assert provider._load().max_seq_length == 512

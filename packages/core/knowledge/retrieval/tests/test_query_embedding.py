"""Pure unit tests (no DB) for the per-turn query embedding cache."""

from __future__ import annotations

from core.knowledge.retrieval.query_embedding import QueryEmbeddingCache
from core.ports.embedding import EmbedRequest


class _CountingProvider:
    model_name = "local/counting"
    dimension = 3

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, req: EmbedRequest) -> list[list[float]]:
        self.calls += 1
        return [[float(len(t)), 0.0, 0.0] for t in req.texts]


async def test_same_query_text_embedded_only_once_per_turn() -> None:
    provider = _CountingProvider()
    cache = QueryEmbeddingCache(provider)

    first = await cache.embed("grappling rules")
    second = await cache.embed("grappling rules")

    assert first == second
    assert provider.calls == 1


async def test_different_query_text_embeds_again() -> None:
    provider = _CountingProvider()
    cache = QueryEmbeddingCache(provider)

    await cache.embed("grappling")
    await cache.embed("stealth")

    assert provider.calls == 2


async def test_new_cache_instance_does_not_share_state() -> None:
    provider = _CountingProvider()
    await QueryEmbeddingCache(provider).embed("grappling")
    await QueryEmbeddingCache(provider).embed("grappling")
    assert provider.calls == 2

import pytest

from adapters.embedding.stub.provider import StubEmbeddingProvider
from core.ports.embedding import EmbedRequest
from core.ports.model_provider import EgressDeniedError


async def test_same_text_always_maps_to_same_vector() -> None:
    provider = StubEmbeddingProvider()
    req = EmbedRequest(model=provider.model_name, texts=["hello", "hello", "world"])
    vectors = await provider.embed(req)
    assert vectors[0] == vectors[1]
    assert vectors[0] != vectors[2]


async def test_vectors_have_the_declared_dimension() -> None:
    provider = StubEmbeddingProvider()
    req = EmbedRequest(model=provider.model_name, texts=["hello"])
    vectors = await provider.embed(req)
    assert len(vectors[0]) == provider.dimension == 8


async def test_egress_denied_before_embedding() -> None:
    provider = StubEmbeddingProvider()
    req = EmbedRequest(
        model=provider.model_name, texts=["hello"], egress_policy={"embed": ["cloud"]}
    )
    with pytest.raises(EgressDeniedError):
        await provider.embed(req)

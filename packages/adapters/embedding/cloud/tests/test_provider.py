from unittest.mock import AsyncMock, MagicMock

import pytest

from adapters.embedding.cloud.provider import LiteLLMEmbeddingProvider
from core.ports.embedding import EmbedRequest
from core.ports.model_provider import EgressDeniedError


async def test_egress_denied_before_any_network_call() -> None:
    """Cloud purpose forbidden by tenant policy must raise before litellm is ever
    imported/called -- no network access, no API key, needed for this test."""
    provider = LiteLLMEmbeddingProvider("openai/text-embedding-3-large", dimension=3072)
    req = EmbedRequest(model=provider.model_name, texts=["hi"], egress_policy={"embed": ["local"]})
    with pytest.raises(EgressDeniedError):
        await provider.embed(req)


async def test_egress_allowed_reaches_litellm(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    response = MagicMock()
    response.data = [{"embedding": [0.1, 0.2, 0.3]}]
    monkeypatch.setattr(litellm, "aembedding", AsyncMock(return_value=response))

    provider = LiteLLMEmbeddingProvider("openai/text-embedding-3-small", dimension=3)
    req = EmbedRequest(model=provider.model_name, texts=["hi"])

    vectors = await provider.embed(req)
    assert vectors == [[0.1, 0.2, 0.3]]

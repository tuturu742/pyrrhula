"""The one place a concrete ``EmbeddingProvider`` adapter is selected at the API layer
(B1.8). Mirrors ``api.model_provider_factory``'s composition-root pattern for the same
port -- deliberately duplicated from ``worker.embedding_provider_factory`` rather than
importing across the api/worker package boundary (Appendix B's layout keeps them
siblings, neither importing the other in production code).
"""

from __future__ import annotations

from adapters.embedding.cloud.provider import LiteLLMEmbeddingProvider
from adapters.embedding.local.provider import SentenceTransformersEmbeddingProvider
from adapters.embedding.stub.provider import StubEmbeddingProvider
from core.config import get_settings
from core.ports.embedding import EmbeddingProvider


class EmbeddingConfigError(Exception):
    """The configured embedding_dimension doesn't match the selected provider's actual
    dimension -- a deployment misconfiguration, caught loudly rather than silently
    producing vectors that don't fit the pgvector column they're written to."""


def get_embedding_provider(model_name: str | None = None) -> EmbeddingProvider:
    settings = get_settings()
    model_name = model_name or settings.embedding_model

    if model_name == "local/stub":
        provider: EmbeddingProvider = StubEmbeddingProvider(dimension=settings.embedding_dimension)
    elif model_name.startswith("local/"):
        provider = SentenceTransformersEmbeddingProvider(
            model_name.removeprefix("local/"), dimension=settings.embedding_dimension
        )
    else:
        provider = LiteLLMEmbeddingProvider(model_name, dimension=settings.embedding_dimension)

    if provider.dimension != settings.embedding_dimension:
        raise EmbeddingConfigError(
            f"embedding_model {model_name!r} has dimension={provider.dimension}, but "
            f"Settings.embedding_dimension={settings.embedding_dimension}"
        )
    return provider

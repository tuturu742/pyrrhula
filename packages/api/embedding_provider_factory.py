"""The one place a concrete ``EmbeddingProvider`` adapter is selected at the API layer
. Mirrors ``api.model_provider_factory``'s composition-root pattern for the same
port -- deliberately duplicated from ``worker.embedding_provider_factory`` rather than
importing across the api/worker package boundary (the package layout keeps them
siblings, neither importing the other in production code).
"""

from __future__ import annotations

from adapters.embedding.cloud.provider import LiteLLMEmbeddingProvider
from adapters.embedding.local.provider import SentenceTransformersEmbeddingProvider
from adapters.embedding.stub.provider import StubEmbeddingProvider
from core.deployment_settings import current_retrieval_models
from core.ports.embedding import EmbeddingProvider


class EmbeddingConfigError(Exception):
    """The configured embedding_dimension doesn't match the selected provider's actual
    dimension -- a deployment misconfiguration, caught loudly rather than silently
    producing vectors that don't fit the pgvector column they're written to."""


def get_embedding_provider(model_name: str | None = None) -> EmbeddingProvider:
    models = current_retrieval_models()
    model_name = model_name or models.embedding_model

    if model_name == "local/stub":
        provider: EmbeddingProvider = StubEmbeddingProvider(dimension=models.embedding_dimension)
    elif model_name.startswith("local/"):
        provider = SentenceTransformersEmbeddingProvider(
            model_name.removeprefix("local/"), dimension=models.embedding_dimension
        )
    else:
        provider = LiteLLMEmbeddingProvider(model_name, dimension=models.embedding_dimension)

    if provider.dimension != models.embedding_dimension:
        raise EmbeddingConfigError(
            f"embedding_model {model_name!r} has dimension={provider.dimension}, but "
            f"the configured embedding_dimension is {models.embedding_dimension}"
        )
    return provider

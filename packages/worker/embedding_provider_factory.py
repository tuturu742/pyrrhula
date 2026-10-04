"""The worker's composition root for ``EmbeddingProvider`` selection. ``local/``
prefixed model names route to a directly-loaded self-hosted model (bge-m3 via
sentence-transformers, or the deterministic test stub); anything else is a cloud model
via LiteLLM. Asserts the selected adapter's declared dimension matches the deployment's
chosen ``embedding_dimension`` at construction time -- a pinned dimension only means
something if a mismatch is a loud, immediate error.
"""

from __future__ import annotations

from adapters.embedding.cloud.provider import LiteLLMEmbeddingProvider
from adapters.embedding.local.provider import SentenceTransformersEmbeddingProvider
from adapters.embedding.stub.provider import StubEmbeddingProvider
from core.deployment_settings import current_retrieval_models
from core.ports.embedding import EmbeddingProvider


class EmbeddingConfigError(Exception):
    """The configured embedding_dimension doesn't match the selected provider's actual
    dimension -- a deployment misconfiguration, caught at startup rather than silently
    producing chunks whose vectors don't fit the pgvector column they're written to."""


# One provider per model: the local provider holds the loaded weights, and a fresh
# instance per call reloaded them on every job that embeds.
_providers: dict[tuple[str, int], EmbeddingProvider] = {}


def get_embedding_provider(model_name: str | None = None) -> EmbeddingProvider:
    models = current_retrieval_models()
    model_name = model_name or models.embedding_model
    cached = _providers.get((model_name, models.embedding_dimension))
    if cached is not None:
        return cached

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
    _providers[(model_name, models.embedding_dimension)] = provider
    return provider

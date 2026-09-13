"""The worker's composition root for ``EmbeddingProvider`` selection (A1.3). ``local/``
prefixed model names route to a directly-loaded self-hosted model (bge-m3 via
sentence-transformers, or the deterministic test stub); anything else is a cloud model
via LiteLLM. Asserts the selected adapter's declared dimension matches
``Settings.embedding_dimension`` at construction time -- "dimension pinned in config" only
means something if a mismatch is a loud, immediate error.
"""

from __future__ import annotations

from adapters.embedding.cloud.provider import LiteLLMEmbeddingProvider
from adapters.embedding.local.provider import SentenceTransformersEmbeddingProvider
from adapters.embedding.stub.provider import StubEmbeddingProvider
from core.config import get_settings
from core.ports.embedding import EmbeddingProvider


class EmbeddingConfigError(Exception):
    """The configured embedding_dimension doesn't match the selected provider's actual
    dimension -- a deployment misconfiguration, caught at startup rather than silently
    producing chunks whose vectors don't fit the pgvector column they're written to."""


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

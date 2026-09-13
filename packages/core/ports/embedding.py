"""EmbeddingProvider port (D9, §13.3, §13.9). Self-hosted embedding (bge-m3, CPU/GPU) is
the default; a cloud adapter exists behind the same port for tenants who opt in, gated by
the D14 egress policy on ``purpose='embed'`` — full-local is a supported deployment mode;
embedding through a paid API must never be *required* (plan §13.9).

Model names use a ``local/`` prefix for self-hosted adapters (``provider_kind`` in
``core.ports.model_provider`` treats that the same as generation's ``ollama/`` prefix) and
a bare provider-qualified name (e.g. ``openai/text-embedding-3-large``) for cloud ones via
LiteLLM.

``dimension`` is a hard property, not inferred after the fact: a chunk row records which
model produced its vector (``knowledge_chunk.embedding_model``, A1.3), and a caller mixing
vectors from two different dimensions must fail loudly at the point of use, not silently
return zero recall.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from core.ports.model_provider import check_egress


class EmbeddingDimensionMismatchError(Exception):
    """A provider returned vectors whose length doesn't match its declared ``dimension``
    — the config/adapter are out of sync, and that's a hard error (A1.3), not a silent
    zero-recall bug at query time."""


@dataclass(frozen=True)
class EmbedRequest:
    model: str
    texts: list[str]
    purpose: str = "embed"
    # Missing purpose key => permissive default, same convention as GenerationRequest.
    egress_policy: Mapping[str, Sequence[str]] = field(default_factory=dict)


class EmbeddingProvider(Protocol):
    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    async def embed(self, req: EmbedRequest) -> list[list[float]]: ...


def validate_dimension(provider: EmbeddingProvider, vectors: list[list[float]]) -> None:
    for vector in vectors:
        if len(vector) != provider.dimension:
            raise EmbeddingDimensionMismatchError(
                f"{provider.model_name!r} declares dimension={provider.dimension} but "
                f"returned a vector of length {len(vector)}"
            )


__all__ = [
    "EmbeddingDimensionMismatchError",
    "EmbedRequest",
    "EmbeddingProvider",
    "check_egress",
    "validate_dimension",
]

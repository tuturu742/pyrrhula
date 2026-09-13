"""Cloud EmbeddingProvider (OpenAI/Voyage/etc via LiteLLM) — for tenants who opt in to
embedding through a paid API. This adapter doesn't decide policy; it enforces it, the same
separation ``LiteLLMModelProvider`` draws for generation: ``check_egress`` runs first, using
whatever ``egress_policy`` the caller (the embedding job, worker-side) attached — a tenant
whose policy restricts ``purpose='embed'`` to local never reaches this adapter's actual
network call.
"""

from __future__ import annotations

from core.ports.embedding import EmbedRequest, check_egress


class LiteLLMEmbeddingProvider:
    def __init__(self, model: str, *, dimension: int) -> None:
        self._model_name = model
        self._dimension = dimension

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed(self, req: EmbedRequest) -> list[list[float]]:
        check_egress(req.purpose, req.model, req.egress_policy)

        import litellm

        response = await litellm.aembedding(model=self._model_name, input=req.texts)
        return [item["embedding"] for item in response.data]

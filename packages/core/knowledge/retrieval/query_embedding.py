"""Query embedding for retrieval: "embed the query once per
turn, cached." A single turn calls ``search_dense`` once per class (rules/lore/misc,
the bucketing), always with the *same* query text — without this, that's one
embedding inference per class instead of one for the whole turn.

This is a per-turn, in-process memo, not the persistent cross-turn cache A1.9 builds
(Redis, keyed on ``(query_hash, scope_set, class, version_set)``) — a new instance per
turn/request is the right lifetime; nothing here is shared or durable.
"""

from __future__ import annotations

from core.ports.embedding import EmbeddingProvider, EmbedRequest


class QueryEmbeddingCache:
    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        egress_policy: dict[str, list[str]] | None = None,
    ) -> None:
        self._provider = provider
        self._egress_policy = egress_policy or {}
        self._cache: dict[str, list[float]] = {}

    async def embed(self, query_text: str) -> list[float]:
        if query_text not in self._cache:
            req = EmbedRequest(
                model=self._provider.model_name,
                texts=[query_text],
                egress_policy=self._egress_policy,
            )
            vectors = await self._provider.embed(req)
            self._cache[query_text] = vectors[0]
        return self._cache[query_text]

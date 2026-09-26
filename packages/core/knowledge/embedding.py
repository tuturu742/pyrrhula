"""Batched embedding for knowledge chunks: finds chunks needing
an embedding (new from ingestion, or stale after a model change), embeds them in batches
through the injected ``EmbeddingProvider`` port, and writes ``embedding``/
``embedding_model`` back. Same ports-and-adapters rule as ``core.knowledge.ingestion``:
this module takes the provider instance as a parameter — which concrete adapter to use is
decided by the worker composition root (``worker.embedding_provider_factory``), never here.

Embedding cache, keyed by ``content_hash``: before paying for inference, check whether
*any* chunk (any entry, any source) already has an embedding for this exact text under
this exact model — "unchanged entries... keep their chunks and vectors across
re-ingestion/version publish" covers the common case (the same draft entry,
re-ingested unchanged) automatically, since its chunk rows are literally untouched; this
cache additionally covers distinct chunks that happen to share identical text (duplicate
boilerplate across entries/sources), where no inference is needed either.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text

from core.ports.embedding import EmbeddingProvider, EmbedRequest, validate_dimension
from core.tenancy.scope import tenant_scope

_DEFAULT_BATCH_SIZE = 32


@dataclass(frozen=True)
class EmbedStats:
    embedded: int
    reused_from_cache: int


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


async def _find_pending_chunks(
    tenant_id: uuid.UUID, model_name: str, *, knowledge_source_id: uuid.UUID | None
) -> list[tuple[uuid.UUID, str, str]]:
    """Rows needing (re-)embedding for this model: never embedded, or embedded under a
    different model tag (a re-embed job's job: iterate embedding_model != current)."""
    async with tenant_scope(tenant_id) as session:
        if knowledge_source_id is not None:
            rows = (
                await session.execute(
                    text(
                        "SELECT c.id, c.text, c.content_hash FROM knowledge_chunk c "
                        "JOIN knowledge_entry e ON e.id = c.entry_id "
                        "WHERE c.tenant_id = :tenant_id "
                        "AND e.knowledge_source_id = :source_id "
                        "AND (c.embedding IS NULL OR c.embedding_model IS DISTINCT FROM :model)"
                    ),
                    {
                        "tenant_id": tenant_id,
                        "source_id": knowledge_source_id,
                        "model": model_name,
                    },
                )
            ).all()
        else:
            rows = (
                await session.execute(
                    text(
                        "SELECT id, text, content_hash FROM knowledge_chunk "
                        "WHERE tenant_id = :tenant_id "
                        "AND (embedding IS NULL OR embedding_model IS DISTINCT FROM :model)"
                    ),
                    {"tenant_id": tenant_id, "model": model_name},
                )
            ).all()
    return [(row[0], row[1], row[2]) for row in rows]


async def _find_cached_embedding(
    tenant_id: uuid.UUID, content_hash: str, model_name: str
) -> list[float] | None:
    async with tenant_scope(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT CAST(embedding AS TEXT) FROM knowledge_chunk "
                    "WHERE tenant_id = :tenant_id AND content_hash = :hash "
                    "AND embedding_model = :model AND embedding IS NOT NULL LIMIT 1"
                ),
                {"tenant_id": tenant_id, "hash": content_hash, "model": model_name},
            )
        ).first()
    if row is None or row[0] is None:
        return None
    return [float(x) for x in row[0].strip("[]").split(",")]


async def _write_embedding(
    tenant_id: uuid.UUID, chunk_id: uuid.UUID, model_name: str, vector: list[float]
) -> None:
    async with tenant_scope(tenant_id) as session:
        await session.execute(
            text(
                "UPDATE knowledge_chunk SET embedding = CAST(:vec AS vector), "
                "embedding_model = :model WHERE id = :id"
            ),
            {"vec": _vector_literal(vector), "model": model_name, "id": chunk_id},
        )


async def embed_chunks(
    tenant_id: uuid.UUID,
    provider: EmbeddingProvider,
    *,
    knowledge_source_id: uuid.UUID | None = None,
    egress_policy: dict[str, list[str]] | None = None,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> EmbedStats:
    """``knowledge_source_id=None`` sweeps every chunk for the tenant (the re-embed job,
    triggered by a model change); given an id, scopes to one source (the job chained
    after the ingestion)."""
    pending = await _find_pending_chunks(
        tenant_id, provider.model_name, knowledge_source_id=knowledge_source_id
    )

    # Group by content_hash, not just per-row: two pending chunks sharing identical text
    # (duplicate boilerplate across entries, or two entries with byte-identical bodies)
    # must only be embedded once between them, not once each -- "reused_from_cache"
    # covers both a pre-existing DB hit *and* a duplicate within this same run.
    hash_to_vector: dict[str, list[float]] = {}
    hash_to_text: dict[str, str] = {}
    chunk_hash_pairs: list[tuple[uuid.UUID, str]] = []
    reused = 0

    for chunk_id, chunk_text, content_hash in pending:
        chunk_hash_pairs.append((chunk_id, content_hash))
        if content_hash in hash_to_vector or content_hash in hash_to_text:
            reused += 1
            continue
        cached = await _find_cached_embedding(tenant_id, content_hash, provider.model_name)
        if cached is not None:
            hash_to_vector[content_hash] = cached
            reused += 1
        else:
            hash_to_text[content_hash] = chunk_text

    hashes_to_embed = list(hash_to_text)
    for i in range(0, len(hashes_to_embed), batch_size):
        batch_hashes = hashes_to_embed[i : i + batch_size]
        req = EmbedRequest(
            model=provider.model_name,
            texts=[hash_to_text[h] for h in batch_hashes],
            egress_policy=egress_policy or {},
        )
        vectors = await provider.embed(req)
        validate_dimension(provider, vectors)
        for content_hash, vector in zip(batch_hashes, vectors, strict=True):
            hash_to_vector[content_hash] = vector

    for chunk_id, content_hash in chunk_hash_pairs:
        vector = hash_to_vector[content_hash]
        await _write_embedding(tenant_id, chunk_id, provider.model_name, vector)

    return EmbedStats(embedded=len(hashes_to_embed), reused_from_cache=reused)

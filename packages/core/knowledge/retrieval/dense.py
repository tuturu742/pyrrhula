"""Dense (vector) retrieval (INV-4): pgvector HNSW cosine search
against ``knowledge_chunk`` with the tenant/scope/class filter pushed into the SQL
``WHERE`` — never fetched broadly and filtered in Python (``tests/`` asserts this via
``EXPLAIN``, proving the plan actually uses the filter, not just that the code has one).

Doesn't reuse ``adapters.vector.pgvector.PgVectorStore``/the generic ``VectorStore`` port,
despite that adapter's own docstring once saying retrieval would "point [it] at the
real knowledge_chunk table". That port's ``VectorSearchResult(payload: dict)`` shape was
built around a single JSONB payload column (``vector_store_item``); ``knowledge_chunk``'s
useful fields are several real columns (``entry_id``, ``version_id``, ``source_id`` via a
join), and WRRF needs ``rank`` — the 1-indexed position within *this* list, which
the generic port has no place for and which only makes sense computed here, at the point
a single query's ordered result list exists. The port/adapter still stand for any future
non-knowledge vector-store consumer; this is a dedicated, richer-shaped read path built
specifically for the knowledge retrieval pipeline.

``version_ids`` is required and pushed down for the same reason ``scope_keys`` is.
``knowledge_chunk`` holds every published version at once — publishing inserts new rows
and leaves the old ones — so "which version does this workspace read" (the
pin-vs-follow) has to be answered *before* the query rather than after it. Retrieval used
to leave that to the caller and no caller ever applied it, which meant a corrected entry
went on being citable in its original wording: the agent cites `k9`, `k9` says what it
says, and nothing in the trace mentions that the text is a version old. Resolve the set
with ``core.knowledge.retrieval.versions.effective_version_ids``.

``entry_key``/``token_count`` are on ``RetrievalHit`` for fusion: WRRF fusion and
bucket-fill need ``token_count`` to know how much budget a hit costs and ``entry_key`` for
the manifest row shape  — cheaper to select them once here than to
re-fetch per hit later.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text

from core.ports.scope import ScopeSet
from core.tenancy.scope import tenant_scope

_DEFAULT_K = 64

# Exposed (not just inlined) so the pushdown test
# (packages/core/knowledge/retrieval/tests/test_dense.py) can run this
# exact text through EXPLAIN -- proving the *actual query issued* filters in SQL,
# not a hand-copied approximation of it that could silently drift from the real one.
DENSE_SEARCH_SQL = (
    "SELECT c.id, c.entry_id, e.knowledge_source_id, c.version_id, e.entry_key, "
    "c.token_count, 1 - (c.embedding <=> CAST(:qvec AS vector)) AS score "
    "FROM knowledge_chunk c "
    "JOIN knowledge_entry e ON e.id = c.entry_id "
    "WHERE c.tenant_id = :tenant_id "
    "AND c.scope_key = ANY(:scope_keys) "
    "AND c.class = :class_ "
    "AND c.version_id = ANY(:version_ids) "
    "AND c.embedding IS NOT NULL "
    # quarantined content is *absent* from retrieval, not merely flagged in it.
    "AND NOT c.quarantined "
    "ORDER BY c.embedding <=> CAST(:qvec AS vector) "
    "LIMIT :k"
)


@dataclass(frozen=True)
class RetrievalHit:
    chunk_id: uuid.UUID
    entry_id: uuid.UUID
    source_id: uuid.UUID
    version_id: uuid.UUID | None
    entry_key: str
    token_count: int
    rank: int
    score: float


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


async def search_dense(
    *,
    tenant_id: uuid.UUID,
    scope_keys: ScopeSet,
    class_: str,
    version_ids: frozenset[uuid.UUID],
    query_embedding: Sequence[float],
    k: int = _DEFAULT_K,
) -> list[RetrievalHit]:
    if not scope_keys:
        # INV-4: required AND non-empty -- an empty set is indistinguishable from "no
        # filter" if it were allowed through.
        raise ValueError("scope_keys must be a non-empty set (INV-4)")
    if not version_ids:
        # Same reasoning as the scope set: empty would read as "every version", which is
        # the one answer that must never be reachable by omission.
        raise ValueError("version_ids must be a non-empty set")

    vector_literal = _vector_literal(query_embedding)
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                text(DENSE_SEARCH_SQL),
                {
                    "tenant_id": tenant_id,
                    "scope_keys": list(scope_keys),
                    "class_": class_,
                    "version_ids": list(version_ids),
                    "qvec": vector_literal,
                    "k": k,
                },
            )
        ).all()

    return [
        RetrievalHit(
            chunk_id=row[0],
            entry_id=row[1],
            source_id=row[2],
            version_id=row[3],
            entry_key=row[4],
            token_count=row[5],
            rank=i + 1,
            score=float(row[6]),
        )
        for i, row in enumerate(rows)
    ]

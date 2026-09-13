"""Sparse (lexical) retrieval via ``tsvector``/``ts_rank_cd`` (plan §6.3 step 3, INV-4,
A1.4) — identical filter signature to ``dense.py`` (tenant/scope/class pushed into the SQL
``WHERE``) and identical result shape, so WRRF (A1.6) can fuse both lists symmetrically.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from core.knowledge.retrieval.dense import RetrievalHit
from core.ports.scope import ScopeSet
from core.tenancy.scope import tenant_scope

_DEFAULT_K = 64

__all__ = ["RetrievalHit", "search_sparse"]

# See dense.DENSE_SEARCH_SQL's docstring note: exposed so the pushdown test can EXPLAIN
# this exact text.
SPARSE_SEARCH_SQL = (
    "SELECT c.id, c.entry_id, e.knowledge_source_id, c.version_id, e.entry_key, "
    "c.token_count, ts_rank_cd(c.tsv, plainto_tsquery('english', :query_text)) AS score "
    "FROM knowledge_chunk c "
    "JOIN knowledge_entry e ON e.id = c.entry_id "
    "WHERE c.tenant_id = :tenant_id "
    "AND c.scope_key = ANY(:scope_keys) "
    "AND c.class = :class_ "
    "AND c.tsv @@ plainto_tsquery('english', :query_text) "
    # G4.6: quarantined content is *absent* from retrieval, not merely flagged in it.
    "AND NOT c.quarantined "
    "ORDER BY score DESC "
    "LIMIT :k"
)


async def search_sparse(
    *,
    tenant_id: uuid.UUID,
    scope_keys: ScopeSet,
    class_: str,
    query_text: str,
    k: int = _DEFAULT_K,
) -> list[RetrievalHit]:
    if not scope_keys:
        raise ValueError("scope_keys must be a non-empty set (INV-4)")

    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                text(SPARSE_SEARCH_SQL),
                {
                    "tenant_id": tenant_id,
                    "scope_keys": list(scope_keys),
                    "class_": class_,
                    "query_text": query_text,
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

"""v1 VectorStore: pgvector. Table and column names are constructor arguments — this
class doesn't own a schema.

Update (A1.4): this stayed pointed at ``vector_store_item``, not ``knowledge_chunk`` as
originally sketched here. ``VectorSearchResult(payload: dict)`` assumes one JSONB payload
column; ``knowledge_chunk``'s useful fields are several real columns plus a join
(``entry_id``, ``version_id``, ``source_id``), and WRRF (A1.6) needs a ``rank`` this
generic shape has no place for. The real knowledge dense-search path is
``core.knowledge.retrieval.dense.search_dense`` — a dedicated, richer-shaped read function
for that one table, not a configuration change to this class. This adapter/port still
stand for any future non-knowledge vector-store consumer.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import text

from core.ports.vector_store import VectorSearchResult
from core.tenancy.scope import tenant_scope


class PgVectorStore:
    def __init__(
        self,
        table: str = "vector_store_item",
        *,
        id_column: str = "id",
        embedding_column: str = "embedding",
        payload_column: str = "payload",
        tenant_id_column: str = "tenant_id",
        scope_key_column: str = "scope_key",
        class_column: str = "class",
    ) -> None:
        self._table = table
        self._id_column = id_column
        self._embedding_column = embedding_column
        self._payload_column = payload_column
        self._tenant_id_column = tenant_id_column
        self._scope_key_column = scope_key_column
        self._class_column = class_column

    async def search(
        self,
        *,
        tenant_id: uuid.UUID,
        scope_keys: frozenset[str],
        class_: str,
        query_embedding: Sequence[float],
        k: int,
    ) -> list[VectorSearchResult]:
        if not scope_keys:
            # INV-4: the scope filter is required and non-empty, not merely present —
            # an empty set is indistinguishable from "no filter" if we let it through.
            raise ValueError("scope_keys must be a non-empty set (INV-4)")

        # pgvector's text input format: '[v1,v2,...]'. Values come from an internally
        # computed query embedding, never user text, so this is safe string building,
        # not a place user input reaches SQL.
        vector_literal = "[" + ",".join(repr(float(x)) for x in query_embedding) + "]"

        query = text(
            f"""
            SELECT {self._id_column}, {self._payload_column},
                   1 - ({self._embedding_column} <=> CAST(:qvec AS vector)) AS score
            FROM {self._table}
            WHERE {self._tenant_id_column} = :tenant_id
              AND {self._scope_key_column} = ANY(:scope_keys)
              AND {self._class_column} = :class_
            ORDER BY {self._embedding_column} <=> CAST(:qvec AS vector)
            LIMIT :k
            """
        )
        async with tenant_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    query,
                    {
                        "qvec": vector_literal,
                        "tenant_id": tenant_id,
                        "scope_keys": list(scope_keys),
                        "class_": class_,
                        "k": k,
                    },
                )
            ).all()

        return [VectorSearchResult(id=row[0], score=float(row[2]), payload=row[1]) for row in rows]

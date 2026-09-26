"""VectorStore port (INV-4). v1 is pgvector; Qdrant is a swap
behind this port, triggered by scale/latency/recall evidence, not before.

``scope_keys`` and ``class_`` are required, defaultless parameters — INV-4: every vector
query carries a scope filter applied via predicate pushdown, and "forgetting" the filter
must be a compile error, not a leak. Post-filtering (fetch candidates, discard out-of-
scope ones in application code) is forbidden: it degrades recall and is a leak surface the
first time a caller forgets to apply it.

This port doesn't own a table. In Phase 1, A1.4 points an adapter at the real
``knowledge_chunk`` table; the pgvector adapter here is written generically (table/column
names are constructor arguments) so that's a configuration change, not a rewrite.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class VectorSearchResult:
    id: uuid.UUID
    score: float
    payload: dict[str, object]


class VectorStore(Protocol):
    async def search(
        self,
        *,
        tenant_id: uuid.UUID,
        scope_keys: frozenset[str],
        class_: str,
        query_embedding: Sequence[float],
        k: int,
    ) -> list[VectorSearchResult]: ...

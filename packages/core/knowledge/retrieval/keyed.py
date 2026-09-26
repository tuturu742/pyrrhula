"""Keyword-activated entries -> chunk-level candidates for WRRF. Activation operates at the
*entry* granularity (an entry either activates or
doesn't); WRRF fuses at the *chunk* granularity (the unit of retrieval, and the manifest's
unit —  lists ``chunk_id`` per row). This is the adapter between them: each
activated entry's chunks (ordinal-ordered) all inherit that entry's activation rank, with
the ordinal as a stable tiebreak so multi-chunk entries don't collide on one rank.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from core.knowledge.activation import ActivatedEntry
from core.knowledge.retrieval.dense import RetrievalHit
from core.tenancy.scope import tenant_scope

# Activation ranks are small (one per entry); this headroom keeps each activated entry's
# chunks in their own contiguous rank band, ordered by ordinal within it, without ever
# colliding with another entry's band.
_RANK_BAND = 1000


async def expand_activated_entries_to_chunks(
    tenant_id: uuid.UUID, activated: list[ActivatedEntry]
) -> list[RetrievalHit]:
    if not activated:
        return []

    entry_ids = [a.entry_id for a in activated]
    rank_by_entry = {a.entry_id: a.rank for a in activated}

    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT c.id, c.entry_id, e.knowledge_source_id, c.version_id, "
                    "e.entry_key, c.token_count, c.ordinal "
                    "FROM knowledge_chunk c "
                    "JOIN knowledge_entry e ON e.id = c.entry_id "
                    "WHERE c.tenant_id = :tenant_id AND c.entry_id = ANY(:entry_ids) "
                    # quarantined content never activates either.
                    "AND NOT c.quarantined "
                    "ORDER BY c.entry_id, c.ordinal"
                ),
                {"tenant_id": tenant_id, "entry_ids": entry_ids},
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
            rank=(rank_by_entry[row[1]] - 1) * _RANK_BAND + row[6] + 1,
            score=1.0 / rank_by_entry[row[1]],
        )
        for row in rows
    ]

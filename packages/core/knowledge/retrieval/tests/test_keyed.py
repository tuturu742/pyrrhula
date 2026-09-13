"""A1.6: activated entries -> chunk-level candidates, against a live Postgres."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from core.knowledge.activation import ActivatedEntry
from core.knowledge.retrieval.keyed import expand_activated_entries_to_chunks
from core.knowledge.retrieval.tests.conftest import seed_chunk, seed_tenant_and_source
from core.tenancy.scope import tenant_scope


async def _entry_id_for_chunk(tenant_id: uuid.UUID, chunk_id: uuid.UUID) -> uuid.UUID:
    async with tenant_scope(tenant_id) as session:
        return (
            await session.execute(
                text("SELECT entry_id FROM knowledge_chunk WHERE id = :id"), {"id": chunk_id}
            )
        ).scalar_one()


async def test_expands_activated_entry_to_its_chunk(db_available: None) -> None:
    tenant_id, source_id = await seed_tenant_and_source("keyed-basic")
    chunk_id = await seed_chunk(
        tenant_id,
        source_id,
        entry_key="grappling",
        body_text="Roll 1d20+STR to grapple.",
        class_="rules",
        scope_key="workspace_public",
    )
    entry_id = await _entry_id_for_chunk(tenant_id, chunk_id)

    activated = [ActivatedEntry(entry_id=entry_id, entry_key="grappling", rank=1, why="keyword")]
    hits = await expand_activated_entries_to_chunks(tenant_id, activated)

    assert len(hits) == 1
    assert hits[0].chunk_id == chunk_id
    assert hits[0].entry_key == "grappling"


async def test_multiple_activated_entries_stay_in_separate_rank_bands(
    db_available: None,
) -> None:
    tenant_id, source_id = await seed_tenant_and_source("keyed-bands")
    first_chunk = await seed_chunk(
        tenant_id,
        source_id,
        entry_key="first",
        body_text="First entry text.",
        class_="rules",
        scope_key="workspace_public",
    )
    second_chunk = await seed_chunk(
        tenant_id,
        source_id,
        entry_key="second",
        body_text="Second entry text.",
        class_="rules",
        scope_key="workspace_public",
    )
    first_entry_id = await _entry_id_for_chunk(tenant_id, first_chunk)
    second_entry_id = await _entry_id_for_chunk(tenant_id, second_chunk)

    activated = [
        ActivatedEntry(entry_id=first_entry_id, entry_key="first", rank=1, why="keyword"),
        ActivatedEntry(entry_id=second_entry_id, entry_key="second", rank=2, why="keyword"),
    ]
    hits = await expand_activated_entries_to_chunks(tenant_id, activated)
    hits_by_chunk = {h.chunk_id: h for h in hits}

    # Rank-1 entry's chunk must outrank (lower rank number) the rank-2 entry's chunk.
    assert hits_by_chunk[first_chunk].rank < hits_by_chunk[second_chunk].rank


async def test_empty_activation_list_returns_no_hits(db_available: None) -> None:
    assert await expand_activated_entries_to_chunks(uuid.uuid4(), []) == []

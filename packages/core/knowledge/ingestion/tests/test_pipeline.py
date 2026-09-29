"""Ingestion acceptance criteria, exercised against a live Postgres."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from core.knowledge.authoring import create_source
from core.knowledge.ingestion.pipeline import UnsupportedFileTypeError, ingest_document
from core.tenancy.scope import tenant_scope
from core.tenancy.seed import seed_dev_tenant


def _word_count(text_: str) -> int:
    return len(text_.split())


async def _setup(slug_prefix: str) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id, _owner_id, _workspace_id = await seed_dev_tenant(
        slug=f"{slug_prefix}-{uuid.uuid4().hex[:8]}"
    )
    source = await create_source(tenant_id, key="core-rules", name="Core Rules", class_="rules")
    return tenant_id, source.id


async def _chunk_rows(tenant_id: uuid.UUID, entry_id: uuid.UUID) -> list:
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT ordinal, text, class, scope_key, content_hash FROM knowledge_chunk "
                    "WHERE entry_id = :entry_id ORDER BY ordinal"
                ),
                {"entry_id": entry_id},
            )
        ).all()
    return rows


async def test_ingest_markdown_creates_entries_and_chunks(db_available: None) -> None:
    tenant_id, source_id = await _setup("ing-md")
    data = b"# Grappling\nRoll 1d20+STR to grapple.\n\n## Stealth\nRoll 1d20+DEX to hide.\n"

    stats = await ingest_document(
        tenant_id,
        source_id,
        filename="core-rules.md",
        data=data,
        class_="rules",
        scope_key="workspace_public",
        count_tokens=_word_count,
    )

    assert stats.entries_created == 2
    assert stats.entries_updated == 0
    assert stats.chunks_created == 2
    assert stats.chunks_updated == 0
    assert stats.chunks_unchanged == 0


async def test_reingesting_same_file_produces_zero_new_chunks(db_available: None) -> None:
    tenant_id, source_id = await _setup("ing-repeat")
    data = b"# Grappling\nRoll 1d20+STR to grapple.\n"

    first = await ingest_document(
        tenant_id,
        source_id,
        filename="core-rules.md",
        data=data,
        class_="rules",
        scope_key="workspace_public",
        count_tokens=_word_count,
    )
    assert first.chunks_created == 1

    second = await ingest_document(
        tenant_id,
        source_id,
        filename="core-rules.md",
        data=data,
        class_="rules",
        scope_key="workspace_public",
        count_tokens=_word_count,
    )
    assert second.chunks_created == 0
    assert second.chunks_updated == 0
    assert second.chunks_unchanged == 1
    # Same entry_key -> same draft row -> "updated" (upsert always touches it), not
    # "created" again.
    assert second.entries_created == 0
    assert second.entries_updated == 1


async def test_reingesting_changed_content_updates_chunks_not_duplicates(
    db_available: None,
) -> None:
    tenant_id, source_id = await _setup("ing-change")
    original = b"# Grappling\nRoll 1d20+STR to grapple.\n"
    changed = b"# Grappling\nRoll 1d20+STR+2 to grapple, with the new house rule.\n"

    await ingest_document(
        tenant_id,
        source_id,
        filename="core-rules.md",
        data=original,
        class_="rules",
        scope_key="workspace_public",
        count_tokens=_word_count,
    )
    second = await ingest_document(
        tenant_id,
        source_id,
        filename="core-rules.md",
        data=changed,
        class_="rules",
        scope_key="workspace_public",
        count_tokens=_word_count,
    )

    assert second.chunks_created == 0
    assert second.chunks_updated == 1
    assert second.chunks_unchanged == 0


async def test_chunks_carry_denormalised_class_and_scope_key(db_available: None) -> None:
    tenant_id, source_id = await _setup("ing-denorm")
    data = b"# Grappling\nRoll 1d20+STR to grapple.\n"

    await ingest_document(
        tenant_id,
        source_id,
        filename="core-rules.md",
        data=data,
        class_="rules",
        scope_key="faction_thieves",
        count_tokens=_word_count,
    )

    # By this source's entries: the library tenant's chunks are visible to every tenant by
    # design, so an unfiltered LIMIT 1 can hand back one of those instead.
    async with tenant_scope(tenant_id) as session:
        row = (
            await session.execute(
                text(
                    "SELECT c.class, c.scope_key FROM knowledge_chunk c "
                    "JOIN knowledge_entry e ON e.id = c.entry_id "
                    "WHERE e.knowledge_source_id = :s LIMIT 1"
                ),
                {"s": source_id},
            )
        ).one()
    assert row[0] == "rules"
    assert row[1] == "faction_thieves"


async def test_shrinking_document_deletes_stale_trailing_chunks(db_available: None) -> None:
    tenant_id, source_id = await _setup("ing-shrink")
    # Each "wordN wordN wordN..." paragraph is 300 words, comfortably over a 30-word
    # target -- three of them guarantee multiple chunks per paragraph.
    long_paragraph = " ".join(f"word{i}" for i in range(300))
    long_doc = f"# Lore\n{long_paragraph}\n\n{long_paragraph}\n\n{long_paragraph}\n".encode()

    first = await ingest_document(
        tenant_id,
        source_id,
        filename="lore.md",
        data=long_doc,
        class_="lore",
        scope_key="workspace_public",
        count_tokens=_word_count,
    )
    assert first.chunks_created > 1

    async with tenant_scope(tenant_id) as session:
        entry_id = (
            await session.execute(
                text(
                    "SELECT id FROM knowledge_entry "
                    "WHERE version_id IS NULL AND knowledge_source_id = :source_id"
                ),
                {"source_id": source_id},
            )
        ).scalar_one()
    before = await _chunk_rows(tenant_id, entry_id)

    short_doc = f"# Lore\n{long_paragraph}\n".encode()
    await ingest_document(
        tenant_id,
        source_id,
        filename="lore.md",
        data=short_doc,
        class_="lore",
        scope_key="workspace_public",
        count_tokens=_word_count,
    )
    after = await _chunk_rows(tenant_id, entry_id)
    assert len(after) < len(before)


async def test_unsupported_file_type_is_rejected(db_available: None) -> None:
    tenant_id, source_id = await _setup("ing-badtype")
    with pytest.raises(UnsupportedFileTypeError):
        await ingest_document(
            tenant_id,
            source_id,
            filename="rules.docx",
            data=b"whatever",
            class_="rules",
            scope_key="workspace_public",
            count_tokens=_word_count,
        )

"""The knowledge repository — the only way stored Knowledge Source/entry/chunk data is
read or written. **INV-1: only `core.assembler` and `core.overseer` may import
this module** — enforced by `tests/architecture/test_inv1_import_graph.py`.

The basic, tenant-scoped "what does a source's current published version look like"
reads — the shape the assembler needs. The hard part (hybrid vector+lexical+keyword
retrieval, scoped and budgeted) lives in ``core.knowledge.retrieval``; this is
deliberately just enough real content that the lint protects a module something actually
calls, rather than an empty stub forever.

Authoring (create/edit/publish) is a *different* module,
``core.knowledge.authoring`` — freely importable by ``api/routes/knowledge.py`` — because
INV-1 protects the "stored text reaches a model" path, not a human author editing their
own tenant's content through the UI. See that module's docstring.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.knowledge.models import KnowledgeEntry, KnowledgeSource
from core.tenancy.scope import tenant_scope


async def get_current_version_id(
    tenant_id: uuid.UUID, knowledge_source_id: uuid.UUID
) -> uuid.UUID | None:
    async with tenant_scope(tenant_id) as session:
        source = await session.get(KnowledgeSource, knowledge_source_id)
        return source.current_version_id if source is not None else None


async def list_published_entries(
    tenant_id: uuid.UUID, version_id: uuid.UUID
) -> list[KnowledgeEntry]:
    """Entries as they stood at a specific, immutable published version — the read the
    assembler will filter by scope_key/class and budget once it exists."""
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(KnowledgeEntry).where(KnowledgeEntry.version_id == version_id)
            )
        ).scalars()
        return list(rows)


async def get_source_names(
    tenant_id: uuid.UUID, source_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """the citation envelope needs the human-readable ``source="..."`` label for
    each of a small, already-budgeted set of chunks -- a lookup by id set, not a full scan."""
    if not source_ids:
        return {}
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(KnowledgeSource.id, KnowledgeSource.name).where(
                    KnowledgeSource.tenant_id == tenant_id, KnowledgeSource.id.in_(source_ids)
                )
            )
        ).all()
    return {row[0]: row[1] for row in rows}


async def get_entry_titles(
    tenant_id: uuid.UUID, entry_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Same shape as ``get_source_names``, for the envelope's ``entry="..."`` label."""
    if not entry_ids:
        return {}
    async with tenant_scope(tenant_id) as session:
        rows = (
            await session.execute(
                select(KnowledgeEntry.id, KnowledgeEntry.title).where(
                    KnowledgeEntry.tenant_id == tenant_id, KnowledgeEntry.id.in_(entry_ids)
                )
            )
        ).all()
    return {row[0]: row[1] for row in rows}


async def get_entry_by_key_and_version(
    tenant_id: uuid.UUID,
    knowledge_source_id: uuid.UUID,
    entry_key: str,
    version_id: uuid.UUID | None,
) -> KnowledgeEntry | None:
    """the citation resolution: a citation records its entry's *pinned*
    version_id at generation time (from the manifest entry that produced it), so
    resolving it later means fetching exactly that immutable published row -- never the
    draft, never whatever the source's ``current_version_id`` has since become, even
    after further publishes. ``version_id=None`` resolves against the current draft
    (an edge case: a citation generated while a source has no published version yet)."""
    async with tenant_scope(tenant_id) as session:
        row = await session.scalar(
            select(KnowledgeEntry).where(
                KnowledgeEntry.tenant_id == tenant_id,
                KnowledgeEntry.knowledge_source_id == knowledge_source_id,
                KnowledgeEntry.entry_key == entry_key,
                KnowledgeEntry.version_id == version_id,
            )
        )
        return row

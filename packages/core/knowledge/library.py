"""The reserved library tenant: a well-known, read-only tenant
holding pack seed content, embedded once rather than once per consuming tenant. Every
other tenant can *read* a library source through the RLS disjunct the
``6b3e9f2d1a47_library_tenant`` migration adds to ``knowledge_source``/``_version``/
``knowledge_entry``/``knowledge_chunk``'s ``tenant_isolation`` policy; nobody but the
library tenant itself can ever write one, because that policy's ``WITH CHECK`` clause is
untouched -- a foreign tenant's write still needs its new row's ``tenant_id`` to equal its
own, which a library row's never does.

``LIBRARY_TENANT_ID`` must stay byte-for-byte identical to the migration's
``_LIBRARY_TENANT_ID`` literal -- ``tests/isolation/test_library_rls_policy.py`` cross-checks
the two.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.knowledge.authoring import (
    EntryFields,
    create_source,
    publish_version,
    upsert_draft_entry,
)
from core.knowledge.models import KnowledgeSource, KnowledgeSourceVersion
from core.knowledge.versioning import fork_source
from core.tenancy.scope import tenant_scope

LIBRARY_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


async def is_library_source(tenant_id: uuid.UUID, source_id: uuid.UUID) -> bool:
    """``tenant_id`` is whoever is asking, not necessarily the library -- a source is
    "library" based on its own ``tenant_id`` column, not the caller's. Works for any
    caller because the RLS disjunct makes a library source visible under every tenant's
    scoped session, not just the library's own."""
    async with tenant_scope(tenant_id) as session:
        source = await session.get(KnowledgeSource, source_id)
        return source is not None and source.tenant_id == LIBRARY_TENANT_ID


async def _find_existing_fork(
    tenant_id: uuid.UUID, library_source_id: uuid.UUID
) -> uuid.UUID | None:
    """A previous edit may have already forked this exact library source for this
    tenant -- reuse it instead of forking again on every subsequent entry edit. Detected
    by walking its own provenance link (``parent_version_id``, recorded on the fork's
    first publish) rather than a new column: a source has no "forked from" pointer of its
    own in this schema, only its *versions* do, which is enough to answer the question."""
    async with tenant_scope(tenant_id) as session:
        library_version_ids = (
            (
                await session.execute(
                    select(KnowledgeSourceVersion.id).where(
                        KnowledgeSourceVersion.knowledge_source_id == library_source_id
                    )
                )
            )
            .scalars()
            .all()
        )
        if not library_version_ids:
            return None

        fork_source_id = await session.scalar(
            select(KnowledgeSourceVersion.knowledge_source_id)
            .where(
                KnowledgeSourceVersion.tenant_id == tenant_id,
                KnowledgeSourceVersion.parent_version_id.in_(library_version_ids),
            )
            .limit(1)
        )
        return fork_source_id


async def fork_if_library(
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    created_by: uuid.UUID | None = None,
) -> uuid.UUID:
    """Fork-on-edit: if ``source_id`` names a library source, returns the id of
    ``tenant_id``'s own editable fork of it (reusing an existing fork if this tenant has
    already made one, otherwise forking fresh via the ``fork_source``) -- the library
    copy is never touched. If ``source_id`` isn't a library source, returns it unchanged;
    this makes the function safe to call unconditionally in front of any entry edit.
    """
    async with tenant_scope(tenant_id) as session:
        source = await session.get(KnowledgeSource, source_id)
    if source is None or source.tenant_id != LIBRARY_TENANT_ID:
        return source_id

    existing_fork_id = await _find_existing_fork(tenant_id, source_id)
    if existing_fork_id is not None:
        return existing_fork_id

    if source.current_version_id is None:
        raise ValueError(f"library source {source_id} has no published version to fork")

    forked = await fork_source(
        tenant_id,
        source_id,
        source.current_version_id,
        new_key=f"{source.key}-fork-{tenant_id.hex[:8]}",
        new_name=f"{source.name} (fork)",
        created_by=created_by,
    )
    return forked.id


async def seed_library_source(
    key: str,
    name: str,
    class_: str,
    entries: list[tuple[str, EntryFields]],
) -> KnowledgeSource:
    """Provisioning path (subtask): embeds a pack's content into the library tenant once.
    A consuming tenant's provisioning never copies this content -- it calls
    ``core.knowledge.authoring.attach_source_to_workspace`` with the resulting source's id,
    which already works unmodified for a library source (the RLS disjunct is what makes
    that read succeed; the attachment row itself is written under the consuming tenant, so
    no write to the library tenant is ever needed to attach). Real pack ingestion (parsing
    a pack's on-disk content into ``entries``) is Phase 3's job -- this is the thin,
    already-sufficient primitive that job will call, not a reimplementation of it now.
    """
    source = await create_source(LIBRARY_TENANT_ID, key=key, name=name, class_=class_)
    for entry_key, fields in entries:
        await upsert_draft_entry(LIBRARY_TENANT_ID, source.id, entry_key, fields)
    await publish_version(LIBRARY_TENANT_ID, source.id, change_note=f"seed: {key}")

    async with tenant_scope(LIBRARY_TENANT_ID) as session:
        refreshed = await session.get(KnowledgeSource, source.id)
        assert refreshed is not None
        return refreshed
